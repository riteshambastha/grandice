# Deploying to AWS

The harness runs directly on the EC2 host (a Python venv, kept alive by
systemd). Docker is installed on that same host, but only to serve as the
sandbox backend for tool execs — the harness process is not itself
containerized.

That split is deliberate, not a shortcut. If the harness ran inside a
container and its sandbox spawned *sibling* containers via the host's Docker
socket, the workspace path inside the harness container (say `/app/workspace`)
would not be a path the host daemon can resolve — it would look for
`/app/workspace` on the EC2 host itself. Making that work requires forcing
identical absolute paths on both sides of the socket, a known
Docker-outside-of-Docker trap. Running the harness directly avoids the problem
outright: one filesystem, no translation.

No GPU is needed. Inference is a hosted API call (OpenRouter's free tier);
the instance only runs the loop, the tools and, per exec, a short-lived
sandbox container.

## 1. Launch the instance

- **AMI**: Amazon Linux 2023
- **Instance type**: `t3.small` is enough (2 vCPU, 2 GB) for the harness
  itself — it is not doing inference locally. Go to `t3.medium` if you plan to
  run several sessions at once.
- **Storage**: 20 GB gp3 is comfortable headroom over the ~1.5 GB the base
  Docker image (`python:3.12-slim`) and the venv take.
- **Security group**: no inbound ports needed for the CLI. Allow SSH (22)
  from your IP only. Nothing needs to be public — this is a client, not a
  server, at P0/P1.
- **IAM role**: none required yet. Grandice does not call any AWS API in this
  phase.

## 2. Install Docker and Python

```bash
sudo dnf install -y docker python3.12 git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# log out and back in (or `newgrp docker`) so the group membership takes effect
docker run --rm hello-world   # confirms the daemon works before anything else does
```

## 3. Clone and install

```bash
git clone https://github.com/riteshambastha/grandice.git
cd grandice
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
docker pull python:3.12-slim   # the default sandbox image — pull once up front
```

## 4. Configure

```bash
cp .env.example .env
chmod 600 .env        # holds your OpenRouter key; keep it out of the repo and off shared perms
```

Edit `.env`:

```bash
GRANDICE_BASE_URL=https://openrouter.ai/api/v1
GRANDICE_API_KEY=sk-or-...

# Free tier: 20 req/min always; 50/day free, 1,000/day after a one-time (not
# recurring) $10 OpenRouter credit purchase. An agentic task can spend 30-120
# calls, so 50/day is exhausted fast — the $10 top-up is worth it.
GRANDICE_DAILY_REQUEST_CAP=1000   # set to 50 if you have not made the top-up

# GRANDICE_SANDBOX is unset here on purpose: Config.from_env() defaults to
# "docker" on Linux automatically. Only set it if you want to override that.
```

`from_env()` picks the Docker sandbox by platform, so nothing else changes
between your Mac and this box.

## 5. Smoke-test the sandbox for real

The Docker sandbox is only unit-tested on macOS dev machines (no Docker
there). This is the first time it runs against a real daemon — verify the
three isolation rules directly, the same way P0's sandbox-exec backend was
checked before anything was built on top of it:

```bash
.venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, "src")
from pathlib import Path
from grandice import sandbox as sb

async def main():
    box = sb.build("docker", Path("workspace"))
    for label, cmd in [
        ("write inside",  "echo hello > ok.txt && cat ok.txt"),
        ("no network",    "curl -s -m 3 https://example.com -o /dev/null && echo REACHED || echo blocked"),
        ("wall-clock cap","sleep 30"),
    ]:
        r = await box.run(cmd, timeout=5)
        print(f"{label:16} exit={r.exit_code:<4} {r.render()[:80]!r}")

asyncio.run(main())
PY
rm -f workspace/ok.txt
```

Expect: the first prints `hello`, the second prints `blocked` (no `REACHED`),
the third exits 124 with `[killed: wall-clock cap reached]`. If any of those
don't hold, do not proceed to running real tasks — file it before trusting the
sandbox.

## 6. Run it

```bash
.venv/bin/grandice "list the workspace"          # one task
.venv/bin/grandice                               # REPL
.venv/bin/pytest -q                              # 28 tests, same suite as local dev
```

## 7. Keep it running (systemd)

Only needed for the REPL/long-lived case — a one-shot task from a script or
cron doesn't need this.

```ini
# /etc/systemd/system/grandice.service
[Unit]
Description=Grandice agent harness
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/grandice
EnvironmentFile=/home/ec2-user/grandice/.env
ExecStart=/home/ec2-user/grandice/.venv/bin/grandice
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grandice
journalctl -u grandice -f
```

## Known limits of this deployment

- **No GPU, none needed.** Everything in §03 of the build spec is a hosted
  API call. If you later self-host open weights instead of using free-tier
  APIs, that is a `g5`/`g6` GPU instance and a model server (vLLM/TGI) —
  a materially different box, not an upgrade to this one.
- **The free tier is the real ceiling, not compute.** 20 req/min and a
  50-or-1,000/day cap apply per OpenRouter account regardless of instance
  size. A bigger instance does not buy you more requests.
- **One instance, one session store.** `.grandice/` (audit log, rate-limit
  state) lives on the instance's own disk. If you need multiple concurrent
  users or horizontal scaling, that is P4-and-later territory (subagents,
  a real queue) — not addressed here.
- **The workspace persists on the instance's EBS volume**, not S3. Fine for
  one user; revisit if the workspace needs to survive an instance
  replacement or be shared.
