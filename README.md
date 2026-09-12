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

The loop, seven tools, a skills loader with two document skills, the router,
two sandbox backends and a CLI — running on OpenRouter's free-tier models.
Everything right of the router is a config string; everything left of it is
here.

| Phase | | |
|---|---|---|
| **P0** | Loop, five tools, CLI | **done** |
| **P1** | Todo tool, container sandbox, free-tier rate limiting, AWS deploy | **done** |
| **P2** | Skills loader, three-tier disclosure | **done** (xlsx, pptx — docx, pdf not yet written) |
| P3 | MCP client, two connectors, `search_tools` | next |
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
.venv/bin/pytest -q                        # 45 tests
.venv/bin/python evals/run.py              # score a model, pass/fail + cost + wall-clock
```

**Deploying to AWS?** See [DEPLOY_AWS.md](DEPLOY_AWS.md) — EC2 sizing, Docker
setup, and why the harness runs directly on the host rather than in its own
container.

## Skills

Two document skills ship: **xlsx** (openpyxl) and **pptx** (python-pptx).
Each is a `skills/<name>/SKILL.md` — frontmatter the model always sees (Tier
1, a couple dozen tokens), a body it pulls in on demand via `load_skill`
(Tier 2), and a helper script it runs but never reads into context (Tier 3,
`scripts/*_inspect.py` — summarises a workbook or deck without dumping it
whole). See `skills/xlsx/SKILL.md` and `skills/pptx/SKILL.md` for the actual
failure modes each one guards against (the `data_only` trap, merged-cell
writes, placeholder indices that don't exist on a given layout, and so on).

Under `sandbox-exec` these libraries come from the harness's own venv — the
sandboxed exec's PATH is pointed at it (see `sandbox._clean_env`). Under the
Docker backend they're baked into `sandbox/Dockerfile` instead, since the
sandbox has no network access to install anything at runtime:

```bash
docker build -t grandice-sandbox:py3.12 sandbox/
```

Adding a third skill: create `skills/<name>/SKILL.md` with a `name` and
`description` in its frontmatter — write the description around concrete
trigger conditions (file extensions, verbs, artefact names), not an abstract
capability summary, so a weaker orchestrator can actually match it (§07).
Nothing else needs registering; `session.build()` discovers it automatically.

## What's here

```
src/grandice/
  loop.py         the agent loop — validate, dispatch, truncate, reflect
  router.py       one interface per provider, cost ledger, failover
  ratelimit.py    free-tier pacing: per-minute window + a persisted daily cap
  sandbox.py      sandbox-exec (macOS) or docker (Linux/EC2) — same interface
  skills.py       three-tier skill discovery + workspace sync (§07)
  context.py      compaction at 70% of the window, into fields not prose
  prompts.py      system prompt and the periodic constraint reminder
  permissions.py  per-action gate that shows the actual payload
  tools/          read, write, edit, glob, bash, todo, load_skill
skills/           xlsx, pptx — canonical source, mirrored into workspace/.skills/
sandbox/          Dockerfile for the sandbox execution image (not the harness)
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
- Only xlsx and pptx are written; docx and pdf are in the original plan but
  not built. Adding one is the same shape (see "Skills" above).
- `sandbox/Dockerfile` needs a manual rebuild after a skill gains a new
  dependency — there's no registry, so this only happens on whatever host
  actually runs the docker sandbox backend.
