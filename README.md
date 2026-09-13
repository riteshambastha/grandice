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

The loop, twelve core tools, a skills loader with four document skills, two MCP
connectors, subagents with a persisted background-task queue, the router, two
sandbox backends, a CLI, a live-view web dashboard, and a native desktop app
for macOS and Windows — running on OpenRouter's free-tier models. Everything
right of the router is a config string; everything left of it is here.

| Phase | | |
|---|---|---|
| **P0** | Loop, five tools, CLI | **done** |
| **P1** | Todo tool, container sandbox, free-tier rate limiting, AWS deploy | **done** |
| **P2** | Skills loader, three-tier disclosure | **done** (xlsx, pptx, docx, pdf) |
| **P3** | MCP client, two connectors, `search_tools` | **done** |
| **P4** | Subagents, background tasks, resumable queue | **done** |
| **P5** | Client — chat, diff, approvals, live task list | **done** (per-file diff, not a full tree; a live list, not a Kanban board — see "Live view") |

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
.venv/bin/pytest -q                        # 233 tests
.venv/bin/python evals/run.py              # score a model, pass/fail + cost + wall-clock
```

**Deploying to AWS?** See [DEPLOY_AWS.md](DEPLOY_AWS.md) — EC2 sizing, Docker
setup, and why the harness runs directly on the host rather than in its own
container.

### A private, self-hosted gateway (e.g. Ollama over Tailscale)

The router is provider-agnostic — anything OpenAI-compatible works, including
a model you run yourself. `GRANDICE_LLM_BASE_URL`/`GRANDICE_LLM_API_KEY`/
`GRANDICE_LLM_CHAT_MODEL`/`GRANDICE_LLM_EMBEDDING_MODEL` are the recognized
vars for this (they take priority over `GRANDICE_BASE_URL`/`GRANDICE_API_KEY`
when both are set — see `config.py`'s `Config.from_env()`), and they go in
**`.env.local`**, not `.env` — a separate file specifically so a private
gateway key never has to sit alongside everything else, gitignored the same
way. Edit `.env.local` directly (it ships with a placeholder key) and never
paste a real key into chat, a commit, or anywhere else that leaves the
machine.

```bash
# .env.local
GRANDICE_LLM_BASE_URL=https://your-tailscale-host.ts.net/v1
GRANDICE_LLM_API_KEY=...                 # your real gateway key, edited in directly
GRANDICE_LLM_CHAT_MODEL=chat
GRANDICE_LLM_EMBEDDING_MODEL=embed       # optional — see Router.embed() in router.py

.venv/bin/python scripts/smoke_test_llm.py   # one real call through Router/Config, proves the wiring
```

Setting only one of `GRANDICE_LLM_BASE_URL`/`GRANDICE_LLM_API_KEY` raises a
clear `ConfigError` at startup rather than silently falling back to
something else — a half-set private-gateway config is almost always a
mistake, not an intentional choice. An **unedited placeholder key is treated
as the whole pair being unset** (not an error, not "live"), so simply
creating `.env.local` from the template never flips a normal run into
hitting a network endpoint on its own.

If the gateway is only reachable via Tailscale (MagicDNS hostnames like
`*.ts.net`), install and connect Tailscale on this machine first — `brew
install tailscale`, then `sudo tailscale up` and sign in with the **same
Tailscale account** the gateway host is on. Verify with `tailscale status`
and by curling the gateway's own health endpoint before pointing grandice at
it. grandice itself never installs or configures Tailscale — that's a
system-level networking change outside what this project touches.

Connection failures, an invalid/revoked key (401), a model the key isn't
permitted to use (403), and a timeout are each reported as a distinct, clear
error rather than one generic failure — see `router.py`'s
`OpenAICompatBackend.complete()`. A self-hosted model can be slow on a cold
start; raise `GRANDICE_LLM_TIMEOUT_SECONDS` (default 60s) if that's a
recurring problem rather than a one-off.

A private box has no external rate limit the way OpenRouter's free tier
does, so `GRANDICE_REQUESTS_PER_MINUTE`/`GRANDICE_DAILY_REQUEST_CAP` are
ignored once a private gateway is what's live, the same reasoning as
`GRANDICE_LLM_CHAT_MODEL` above — this repo's own `.env` has both set to
OpenRouter-tuned values that would otherwise silently throttle a box that
has no such limit at all (caught by actually watching the dashboard report
"1 / 50 requests" against the real gateway).

## Live view

A web dashboard for actually watching the agent work, rather than reading a
terminal log — streamed responses, a live tool-call feed with real diffs on
every file change, in-browser approval prompts for outward-facing tools, the
current plan, a live-updating background-task list, cost and rate-limit
meters, the skills catalog, and a workspace file browser with a preview pane.
Send a task or cancel one from the page; any number of browser tabs can
watch the same chat at once.

```bash
.venv/bin/pip install -e ".[web]"
.venv/bin/grandice-web            # http://127.0.0.1:8000
```

It's a FastAPI app streaming Server-Sent Events to a static page — no build
step, no JS framework, nothing to compile.

**Accounts, projects, chats.** The dashboard requires logging in (real
bcrypt-hashed passwords, not a name picker — see `accounts.py`), and each
account has its own **projects**, each project its own workspace/sandbox
directory, each holding any number of **chats**. A chat is one persisted
conversation: its message history is saved after every turn (`projects.py`),
so closing the dashboard and coming back later — even after a server
restart — resumes the real conversation, not just a blank session. Opening a
chat builds it a `Session` on first touch (its own sandbox, its own event
stream, its own running/approval state); two chats never share state, and
one user's projects are invisible to another's — the whole reason this
needed real authentication rather than a "pick a name" switcher was a small
team using the same install, not just one person. There is deliberately no
"create agents" UI yet — subagents remain something a running task spawns
for itself (§P4), not something a person configures ahead of time from the
dashboard.

**A real limit worth stating plainly.** Login is real, but the network
transport isn't secured by this alone — if the dashboard is reachable over
plain HTTP by more than the one machine it runs on, credentials and every
request are visible to anyone on that network path. Put it behind HTTPS (a
reverse proxy) or a VPN before treating "small team, shared instance" as
more than a local/trusted-network feature. See `accounts.py`'s own module
docstring for the same point in more detail.

**Diffs.** Every successful `write`/`edit` call produces a real unified diff
(`difflib`, computed in `loop.py` around the tool call — before/after content,
not a guess), rendered as its own colored block in the log. A brand-new file
shows its content directly instead of a diff against nothing.

**Approvals.** `fetch` (§P3's outward-facing connector tool) now works from
the dashboard: the turn pauses, an approval modal shows the actual payload —
the real URL, not "the agent wants to fetch something" — and Approve/Deny
resolves it. This reuses the exact same `Gate`/`Risk.OUTWARD` mechanism the
CLI's terminal prompt already used (see `permissions.py`); only the *asker*
differs — an SSE event plus a pending `Future` that `POST /api/approve`
resolves, instead of blocking on `input()`. An unanswered approval denies
itself after 5 minutes rather than hanging the turn forever.

**Background tasks, live.** `spawn_background` (§P4) runs outside the SSE
broadcaster entirely — a lightweight poller (every 2s) is what makes a task's
completion show up without a manual refresh.

**Rich chat.** Modeled after the Claude cowork/chat experience:

- **Model selector** — a dropdown in the topbar, populated by actually
  asking the provider (`GET /v1/models` via `Router.list_models()`), not a
  hardcoded guess; falls back to the configured tiers if the provider
  doesn't support that endpoint. A choice persists as that chat's own
  default (`projects.py`'s `chats.model` column) and survives a reload;
  different chats can run different models at once.
- **File attachments** — attach via the 📎 button or drag-and-drop
  anywhere onto the chat. Every upload lands in that chat's own
  `workspace/uploads/` (server/app.py's `/upload` route, filename
  sanitized against path traversal), so `read`/`glob` can reach it like any
  other workspace file; an image is *also* embedded directly into the
  message as a real `image_url` content part (see `loop.py`'s
  `_build_message_content`), for a model that actually has vision.
- **Voice input** — the 🎤 button uses the browser's own
  `SpeechRecognition` API for dictation into the message box. No server
  round-trip, no model call. Chrome/Edge only (Firefox/Safari don't
  implement it); the button disables itself with an explanatory title
  where it isn't supported, rather than pretending to work.
- **Markdown rendering** — assistant replies render through `marked`
  (vendored, not npm-installed — `server/static/vendor/`) and are
  sanitized through `DOMPurify` before hitting the DOM, since that text
  ultimately comes from a model; verified directly that a `<script>`/
  `onerror` injection attempt gets stripped while real formatting survives.

None of this touches the plain `grandice` CLI or the underlying loop/tool
architecture — it's additive to the dashboard's existing chat surface.

This is not the full P5 client the build spec describes to the letter — no
multi-file tree-wide diff view (one file's diff at a time, as it happens, is
what's here), and the task list is a live list, not a Kanban-style board.
Both are extensions of what already exists rather than new architecture; see
`src/grandice/server/` for the seam.

Binds to `127.0.0.1` by default — nothing is exposed even on an EC2 box unless
you deliberately pass `--host`. To view a remote instance, tunnel instead of
opening a port: `ssh -L 8000:localhost:8000 <host>`, then browse
`http://127.0.0.1:8000` locally.

## Desktop app

The same dashboard as a native window instead of a browser tab — one
process, `pywebview` opening the OS's own webview onto a FastAPI server
embedded in a background thread. Nothing platform-specific in the code;
pywebview picks WKWebView, WebView2 or GTK WebKit itself.

```bash
.venv/bin/pip install -e ".[web,desktop]"
.venv/bin/grandice-desktop
```

A standalone packaged app (`.app` on macOS, `.exe` on Windows), with a real
icon, builds from one PyInstaller spec for both: `./desktop/build_mac.sh`
or `.\desktop\build_windows.ps1`. `cp -R dist/grandice.app /Applications/`
afterward makes it a real double-clickable icon in Applications, Launchpad
and Spotlight. The macOS build is verified — built, launched via `open` (the
actual double-click path, not just running the binary from a terminal —
that distinction is what caught a real bug: Finder launches a GUI app with
cwd `/`, and the workspace default used to be a relative path that resolved
to an unwritable `/workspace` there, failing silently with no terminal to
show it), and checked over real HTTP that it found all four skills and
every tool from inside the bundle alone. The Windows path is written to be
correct but **not verified the same way** — this dev environment is
macOS-only. See [DESKTOP_APP.md](DESKTOP_APP.md) for exactly what that
distinction means, the frozen-app fixes packaging required, and a real,
pre-existing gap worth knowing before relying on a Windows build: there's
no lightweight sandbox for Windows yet,
so it either needs Docker Desktop (the existing default) or runs
unsandboxed.

## Skills

Four document skills ship: **xlsx** (openpyxl), **pptx** (python-pptx),
**docx** (python-docx), **pdf** (pypdf). Each is a `skills/<name>/SKILL.md` —
frontmatter the model always sees (Tier 1, a couple dozen tokens), a body it
pulls in on demand via `load_skill` (Tier 2), and a helper script it runs but
never reads into context (Tier 3, `scripts/*_inspect.py` — summarises a
file without dumping it whole). See each skill's own `SKILL.md` for the
failure modes it guards against, each one verified against the real library
before being written down, not assumed: the `data_only` formula-caching trap
and merged-cell writes (xlsx); placeholder indices that don't exist on a
given slide layout (pptx); `doc.paragraphs` silently excluding table and
header/footer text, and reading order being lost between paragraphs and
tables unless you walk the body XML directly (docx, both confirmed by
actually building a document and reading it back); `PdfMerger` not existing
in the currently pinned pypdf version — merging is a `PdfWriter.append()`
call now — and no OCR, ever, so a scanned page returns near-empty text with
no error at all rather than failing loudly (pdf).

Under `sandbox-exec` these libraries come from the harness's own venv — the
sandboxed exec's PATH is pointed at it (see `sandbox._clean_env`). Under the
Docker backend they're baked into `sandbox/Dockerfile` instead, since the
sandbox has no network access to install anything at runtime:

```bash
docker build -t grandice-sandbox:py3.12 sandbox/
```

Adding a fifth skill: create `skills/<name>/SKILL.md` with a `name` and
`description` in its frontmatter — write the description around concrete
trigger conditions (file extensions, verbs, artefact names), not an abstract
capability summary, so a weaker orchestrator can actually match it (§07).
Nothing else needs registering; `session.build()` discovers it automatically.

## Connectors

Two MCP connectors, off by default:

```bash
.venv/bin/pip install -e ".[mcp]"
GRANDICE_MCP_CONNECTORS=fetch,sqlite .venv/bin/grandice
```

- **fetch** — fetches a URL, converts to markdown. Gated behind the
  permission prompt (`Risk.OUTWARD`): unlike everything else in the
  sandbox, it genuinely leaves the machine — connectors attach at the tool
  layer, outside the network-denied sandbox, which is the only way `fetch`
  can work at all (§07).
- **sqlite** — read/write/create-table/list/describe against one local
  `.db` file in the workspace. Not gated, same trust level as the built-in
  `read`/`write` tools.

Neither is active by default even when configured. Both start **latent** —
discoverable but not shown to the model — until it calls
`search_tools(query)`, at which point matches are activated for the rest of
the session and become callable from the next turn. This is the same
progressive-disclosure trick `load_skill` uses for instructions (§05.2,
§07), applied to tools: a connector can add a handful of tools at once, and
this is what keeps them out of context until something actually needs one.

**A real compatibility note, not a hypothetical one:** `mcp` is pinned to
the 1.x line in `pyproject.toml`, not the newest available. Verified live
(2026-09-12): `mcp` 2.x renamed several `mcp.types` fields
(`Tool.inputSchema` → `input_schema`, `CallToolResult.isError` → `is_error`)
and removed `McpError` outright — and `mcp-server-fetch`, even its current
release at the time, still imports the old name and cannot run on 2.x at
all. Both connectors here work correctly against the pinned 1.x version;
re-verify field names directly against whatever's actually installed before
ever bumping it (see `mcp_client.py`'s module docstring).

Adding a third connector: `mcp_client.spec_for()` needs the new server's
launch command, and `_RISK_OVERRIDES` needs each of its tools classified —
verify by actually starting the server and calling `list_tools()`/
`call_tool()` against it, the same way these two were checked, rather than
assuming risk or schema shape from documentation.

## Subagents & background tasks

`spawn_subagent(task, tools=None)` runs a bounded piece of work in its own
isolated context — its own message history, its own plan — and returns only
the final report. The point is keeping a side-investigation out of the
orchestrator's own window: a twenty-step detour into "what's actually in
these twelve files" shows up as one tool result, not twenty turns of noise.
Runs on the **worker** tier by default (§03's own tier table — bounded,
mechanical work doesn't need the orchestrator's reasoning budget), and
shares the parent's `Router` rather than getting a fresh one, so the cost
ledger and rate limiter correctly aggregate across a session and everything
it spawns.

Two hard rules, enforced in `subagents.py`, not left to the caller's
judgment: a subagent never receives an outward-facing tool regardless of
what's requested (§08 — "a tool set with no outward-facing capability at
all"), and never receives the spawn/task tools themselves — no nested
subagents in this build, a flat one-level hierarchy on purpose.

`spawn_background(task, tools=None)` is the same thing without waiting —
returns a task id immediately; check on it with `check_task(task_id)` or see
everything with `list_tasks()` (or `/tasks` in the CLI, or the "Background
tasks" panel in the dashboard). Tasks persist in `.grandice/tasks.db`
(SQLite — §09's own stack pick for exactly this), shared across every
session against a workspace, so a task started in one run still shows up
after a restart.

**"Resumable" is scoped honestly, not oversold.** A task's record — status,
description, result — survives a process restart; one caught `running` when
the process dies is marked `interrupted` on the next startup rather than
silently lost. It does **not** mean the exact in-progress conversation picks
back up mid-transcript — that would need checkpointing the subagent's
message list at every step, which isn't built. "Resuming" an interrupted
task today means `spawn_background`-ing the same description again, with
full knowledge it didn't finish last time.

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
  mcp_client.py   MCP connectors — start a server, wrap its tools as latent ToolSpecs
  subagents.py    isolated child sessions, sharing the parent's router + sandbox
  tasks.py        SQLite-backed background task queue (.grandice/tasks.db)
  tools/          read, write, edit, glob, bash, todo, load_skill, search_tools,
                  spawn_subagent, spawn_background, check_task, list_tasks
  server/         FastAPI + SSE live-view dashboard (app.py, broadcast.py, static/)
  desktop.py      native window shell — embeds server/app.py via pywebview
skills/           xlsx, pptx, docx, pdf — canonical source, mirrored into workspace/.skills/
sandbox/          Dockerfile for the sandbox execution image (not the harness)
desktop/          PyInstaller spec + build scripts for the standalone app
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

- Voice input needs the browser's own `SpeechRecognition` API — Chrome/Edge
  only; the mic button disables itself elsewhere rather than pretending to
  work. No server-side speech-to-text fallback.
- Uploaded attachments have no automatic cleanup — they sit in that chat's
  `workspace/uploads/` indefinitely, the same as any other workspace file.
- `AccountStore`/`ProjectStore` each guard their one shared SQLite
  connection with a single lock (a real bug, found live: concurrent
  requests without one produced both a `TypeError` and a raw
  `sqlite3.InterfaceError` from two threads' queries interleaving) — this
  serializes correctness, not throughput; a small team's actual request
  volume is nowhere near where that would matter, but it's not built for
  heavy concurrent write load.
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
- The pdf skill cannot author new richly-formatted content, and does no
  OCR — see the skill's own opening section, stated up front rather than
  discovered mid-task. Both are pypdf's actual limits, not an oversight.
- `sandbox/Dockerfile` needs a manual rebuild after a skill gains a new
  dependency — there's no registry, so this only happens on whatever host
  actually runs the docker sandbox backend.
- The dashboard requires login, but that secures *who's who*, not the wire —
  binding to `127.0.0.1` by default is still the only transport protection.
  Do not point `--host` at a public interface without HTTPS or a VPN in
  front of it; a plain reachable HTTP dashboard leaks passwords and every
  request to anyone on that network path. See "Accounts, projects, chats"
  above.
- Each **chat** runs one top-level task at a time — multiple browser tabs
  can watch the same chat, but not start independent top-level tasks in it
  concurrently. Different chats are fully independent, though, each with its
  own `Session` and its own concurrency; `spawn_background` genuinely runs
  work concurrently underneath a chat's one top-level task, which is real
  progress from before P4, but the per-chat top-level constraint itself is
  still there.
- `mcp` is pinned to 1.x, not the newest release — see "Connectors" above.
  This ecosystem is moving fast enough that a routine `pip install --upgrade`
  could silently break both connectors; the pin is deliberate, not an
  oversight, and bumping it needs the same live re-verification that found
  the incompatibility in the first place.
- Only two connectors exist (fetch, sqlite); the doc's own examples (Drive,
  Slack, mail, Postgres) all need real credentials this build doesn't handle
  yet. Adding one is the same shape (see "Connectors" above), but a
  credentialed connector also needs a place to hold the credential — not
  designed here.
- Subagents are a flat, one-level hierarchy — they cannot themselves spawn
  subagents. Deliberate scope for this pass, not a technical ceiling; lifting
  it is mostly relaxing `_RECURSIVE_TOOL_NAMES` in `subagents.py`, but that
  also reopens the runaway-recursive-cost question this restriction sidesteps.
- A background task's completion reaches the dashboard through a 2-second
  poller (`server/app.py`'s `_poll_tasks`), not an instant push — `spawn_
  background` runs outside the SSE broadcaster entirely, since tools stay
  unaware of the web layer on purpose. Close enough to live for a task board;
  not the same guarantee the main task's own events have.
- `.grandice/tasks.db` has no pruning — it grows forever. Fine at the scale
  this has been used at; revisit if it matters.
- The diff view covers `write`/`edit` only — a file changed some other way
  (bash redirecting output, an MCP connector) produces no diff. Best-effort,
  not a filesystem watcher.
- An unanswered approval denies itself after 5 minutes (`APPROVAL_TIMEOUT_
  SECONDS`) rather than hanging the turn forever — reasonable for a human
  who stepped away, but it does mean walking away from the dashboard mid-
  approval silently declines the action rather than leaving it pending.
- The diff view and approval modal are single-file/single-request — no
  tree-wide "review everything this turn changed" view, and approvals queue
  one at a time rather than showing several at once.
- The desktop app's Windows build is written to be correct but not
  verified the same way the macOS one was (no Windows machine in this dev
  environment) — see [DESKTOP_APP.md](DESKTOP_APP.md). Same document for
  the real, pre-existing gap it surfaces: no lightweight Windows sandbox,
  so a Windows install either needs Docker Desktop or runs unsandboxed.
- No code signing on either desktop build — expect a Gatekeeper/SmartScreen
  warning on first launch. Normal for unsigned early-stage software, not
  something fixed here (needs paid certificates on both platforms).
