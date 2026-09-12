# Grandice

A Cowork-class agentic workspace — file-native, sandboxed, plan-driven — running
on open-weight models you don't control the training of.

The interesting engineering is not the chat box. It is everything that keeps a
weaker model coherent across a two-hundred-step task. Roughly 80% of the
perceived quality comes from the runtime, not the model, which is the part open
weights don't take away.

Built to [the build spec](https://claude.ai/code/artifact/1e40174c-9694-47d1-a825-217df91c495c);
section references in the code (§05.3 and so on) point back at it.

## Status

The loop, six tools, the router, two sandbox backends and a CLI — running on
OpenRouter's free-tier models. Everything right of the router is a config
string; everything left of it is here.

| Phase | | |
|---|---|---|
| **P0** | Loop, five tools, CLI | **done** |
| **P1** | Todo tool, container sandbox, free-tier rate limiting, AWS deploy | **done** (documents still open) |
| P2 | Skills loader, three-tier disclosure | next |
| P3 | MCP client, two connectors, `search_tools` | |
| P4 | Subagents, background tasks, resumable queue | |
| P5 | Client — chat, file tree, diff, task board | |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/grandice "list the workspace and summarise sample.csv"
```

With no `GRANDICE_API_KEY` the router runs a **stub backend**: the loop, tools,
validation, truncation, sandbox and cost ledger are all real, only the model is
scripted. Point it at something real when you have a key:

```bash
cp .env.example .env      # add GRANDICE_API_KEY, then:
.venv/bin/grandice        # REPL
```

Defaults run on OpenRouter's **free tier** — zero cost per token, but rate
limited to 20 requests/min and 50/day (1,000/day after a one-time $10 credit
purchase, which is worth making: a single agentic task can spend 30-120 calls).
The router paces calls under that limit itself and stops cleanly, not with a
wall of 429s, once the day's budget is spent.

```bash
.venv/bin/grandice --model <id> "..."      # swap orchestrator, any OpenAI-compatible id
.venv/bin/grandice --sandbox docker "..."  # force the container backend on macOS too
.venv/bin/pytest -q                        # 28 tests
.venv/bin/python evals/run.py              # score a model, pass/fail + cost + wall-clock
```

**Deploying to AWS?** See [DEPLOY_AWS.md](DEPLOY_AWS.md) — EC2 sizing, Docker
setup, and why the harness runs directly on the host rather than in its own
container.

## What's here

```
src/grandice/
  loop.py         the agent loop — validate, dispatch, truncate, reflect
  router.py       one interface per provider, cost ledger, failover
  ratelimit.py    free-tier pacing: per-minute window + a persisted daily cap
  sandbox.py      sandbox-exec (macOS) or docker (Linux/EC2) — same interface
  context.py      compaction at 70% of the window, into fields not prose
  prompts.py      system prompt and the periodic constraint reminder
  permissions.py  per-action gate that shows the actual payload
  tools/          read, write, edit, glob, bash, todo
evals/            pass/fail tasks with mechanical checks — run on every model swap
```

## The nine defences

Each is a failure mode a weaker model hits within a day of real work. They are
what separates this from a chatbot with a shell.

1. **Arguments validated against JSON Schema, always** — invalid calls come back
   as a specific error the model can act on, never as a crashed turn.
2. **Active tool set capped at 15** — the registry refuses to exceed it rather
   than trusting us to be disciplined.
3. **Compaction into fixed fields** — goal, decisions, files, findings, open,
   next. Freeform summaries lose the thing the model needed.
4. **Tool results truncated to ~2k tokens with a pointer** — the full output goes
   to disk and the model is told the path.
5. **A todo tool from day one** — the largest single lever on long-horizon
   coherence, and it costs eighty lines.
6. **Constraints re-injected every few turns** — open models drift from the
   system prompt well before their context limit.
7. **Line-range edits with a content hash** — exact-string-replace is right for a
   frontier model and a trap for everything else. A stale edit returns the
   current contents instead of corrupting the file.
8. **Forced reflection after two consecutive failures** — breaks retry loops for
   the price of one cheap turn.
9. **Temperature 0.2 on the orchestrator** — plus constrained decoding wherever a
   provider offers it.

## Trust boundary

This is more dangerous than a coding agent: the job is ingesting content
strangers wrote while holding credentials. P0 assumes the worst of everything it
reads — tool output is wrapped as data and never authoritative, the sandbox has
no network egress, outward actions stop for a human with the payload shown, and
every tool call is logged with its arguments to `.grandice/<session>.jsonl`.

`evals/tasks/injection-resistance` is a live test of that: a file that instructs
the agent to ignore its instructions. It must summarise the file, not obey it.

## Known limits

- `sandbox-exec` (macOS) is a deprecated Apple API, weak against a determined
  escape; the Docker backend (Linux/EC2) is the same trust level, containers
  rather than a seatbelt profile. Neither is Firecracker-grade isolation.
- Free-tier models rotate — a model that resolves today may be repriced or
  pulled tomorrow. The router will simply error; re-check
  openrouter.ai/models and update `.env` when it does.
- Session state is a JSONL audit log, not a database. SQLite arrives with
  resumable tasks in P4.
- No prompt caching yet — moot on the free tier, but a real cost lever the
  day a paid orchestrator is added.
- Token counts are estimated at 4 chars/token for budgeting, not billing.
- Docker sandbox isolation flags are unit-tested against the constructed
  command, not a live daemon (none runs on the macOS dev machine) — the first
  real check is the smoke test in `DEPLOY_AWS.md` §5, on the actual EC2 host.
