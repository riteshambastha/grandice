"""The agent loop (§04).

Four properties matter more than the code. Every failure returns to the model as
a tool result rather than raising. Validation happens before execution. Risky
tools stop for a human. Results are truncated on the way in, not the way out.
"""

from __future__ import annotations

import difflib

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from . import context
from .ratelimit import DailyCapReached
from .router import CostCapExceeded, Reply, ToolCall
from .session import Session
from .tools.base import Risk, tool_error, tool_result, truncate, validate

REFLECT_AFTER = 2  # consecutive failures on one tool before a forced rethink

# Tools that change a file's contents on disk — the ones worth diffing.
# Anything else (bash writing a file some other way, an MCP connector) isn't
# covered; the diff view is best-effort, not a filesystem watcher.
_FILE_WRITING_TOOLS = {"write", "edit"}
DIFF_CHAR_LIMIT = 4_000  # same truncate-on-the-way-in spirit as §05.4


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolStarted:
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolFinished:
    name: str
    ok: bool
    preview: str


@dataclass
class FileChanged:
    path: str
    diff: str  # a unified diff; "(no textual difference)" if none, "(new file)" if before was absent


@dataclass
class Finished:
    reason: str
    steps: int
    cost_usd: float


Event = TextDelta | ToolStarted | ToolFinished | FileChanged | Finished


async def run_turn(
    session: Session, user_message: str, tier: str = "orchestrator"
) -> AsyncIterator[Event]:
    """One user message to completion. Yields events for the client to render.

    `tier` picks which model tier drives this turn (§03) — subagents (§P4)
    run on "worker" by default, since bounded, mechanical work doesn't need
    the orchestrator's own reasoning budget. The router instance is shared
    with the parent session regardless of tier, so the cost ledger and rate
    limiter aggregate correctly across a session and any subagents it spawns.
    """
    session.messages.append({"role": "user", "content": user_message})
    session.turns += 1
    session.log("user", content=user_message)

    reason = "done"

    while session.steps < session.config.max_steps and not session.cancelled:
        if context.needs_compaction(session):
            await context.compact(session)

        reply: Reply | None = None
        try:
            # Streamed straight through: text reaches the client as the model
            # produces it, not after the turn resolves.
            async for item in session.router.complete(
                tier=tier,
                messages=context.assemble(session),
                tools=context.active_tools(session),
            ):
                if isinstance(item, str):
                    yield TextDelta(item)
                else:
                    reply = item
        except (CostCapExceeded, DailyCapReached) as exc:
            reason = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            session.log("router_error", error=str(exc))
            reason = f"router failed: {exc}"
            break

        if reply is None:
            reason = "backend produced no reply"
            break

        session.messages.append(reply.message)
        session.log("assistant", text=reply.text, tool_calls=[tc.name for tc in reply.tool_calls])

        if not reply.tool_calls:
            break  # the model is done talking

        for call in reply.tool_calls:
            before = _snapshot_if_relevant(session, call)

            yield ToolStarted(call.name, call.arguments)
            result = await execute(session, call)
            content = result["content"]
            failed = content.startswith("ERROR:") or content.startswith("STALE:")
            yield ToolFinished(call.name, not failed, _preview(content))
            session.messages.append(result)

            if before is not None and not failed:
                yield _diff_event(session, call.arguments["path"], before)

            if failed and session.note_failure(call.name) >= REFLECT_AFTER:
                session.messages.append(_reflection(call.name))
                session.log("reflection", tool=call.name)
            elif not failed:
                session.note_success(call.name)

        session.steps += 1
    else:
        if session.cancelled:
            reason = "interrupted"
        elif session.steps >= session.config.max_steps:
            reason = f"hit the step cap ({session.config.max_steps})"

    yield Finished(reason, session.steps, session.router.ledger.spent_usd)


async def execute(session: Session, call: ToolCall) -> dict[str, Any]:
    """Dispatch one tool call. Never raises — the model must see what went wrong."""
    session.log("tool_call", tool=call.name, arguments=call.arguments)

    spec = session.registry.get(call.name)
    if spec is None:
        return tool_error(
            call.id,
            call.name,
            f"No tool named {call.name!r}. Available: {', '.join(session.registry.names())}.",
        )

    ok, message = validate(call.arguments, spec.schema)
    if not ok:
        return tool_error(call.id, call.name, f"Invalid arguments: {message}. Fix and retry.")

    if spec.risk is Risk.OUTWARD and not await session.gate.allows(spec, call.arguments):
        return tool_error(
            call.id, call.name, "User declined this action. Ask them what to do instead."
        )

    try:
        output = await spec.run(**call.arguments)
    except PermissionError as exc:
        return tool_error(call.id, call.name, str(exc))
    except Exception as exc:  # noqa: BLE001 — a tool bug is a model-visible event
        return tool_error(
            call.id, call.name, f"{type(exc).__name__}: {exc}. Diagnose before retrying."
        )

    trimmed = truncate(
        output,
        budget_tokens=session.config.tool_result_budget,
        overflow_dir=session.overflow_dir,
        label=call.name,
    )
    session.log("tool_result", tool=call.name, chars=len(output), truncated=len(trimmed) < len(output))
    return tool_result(call.id, call.name, trimmed)


def _snapshot_if_relevant(session: Session, call: ToolCall) -> str | None:
    """The file's content before a write/edit call, so a diff can be shown
    after (§P5). `None` means "not a file-writing call, nothing to diff" —
    distinct from `""`, which means "the file didn't exist yet"."""
    if call.name not in _FILE_WRITING_TOOLS:
        return None
    path = call.arguments.get("path")
    if not isinstance(path, str):
        return None
    try:
        target = session.sandbox.resolve(path)
        return target.read_text() if target.exists() else ""
    except Exception:  # noqa: BLE001 — best-effort; a real problem surfaces via the tool call itself
        return ""


def _diff_event(session: Session, path: str, before: str) -> FileChanged:
    try:
        after = session.sandbox.resolve(path).read_text()
    except Exception:  # noqa: BLE001
        return FileChanged(path, "(could not read the file back to diff it)")

    if not before:
        preview = after[:DIFF_CHAR_LIMIT]
        suffix = (
            "" if len(after) <= DIFF_CHAR_LIMIT
            else f"\n... [{len(after) - DIFF_CHAR_LIMIT:,} more characters]"
        )
        return FileChanged(path, f"(new file)\n{preview}{suffix}")

    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))
    text = "".join(diff_lines)
    if not text:
        return FileChanged(path, "(no textual difference)")
    if len(text) > DIFF_CHAR_LIMIT:
        text = text[:DIFF_CHAR_LIMIT] + f"\n... [diff truncated, {len(text) - DIFF_CHAR_LIMIT:,} more characters]"
    return FileChanged(path, text)


def _reflection(tool: str) -> dict[str, Any]:
    """Break a retry loop cheaply (§05.8). A forced reasoning step costs one
    cheap turn; a model hammering the same broken call costs the session."""
    return {
        "role": "user",
        "content": (
            f"<system_note>\n`{tool}` has now failed twice in a row. Before calling it "
            f"again, state in one or two sentences why it failed and what you will do "
            f"differently. If you do not have a different approach, say so and stop.\n"
            f"</system_note>"
        ),
    }


def _preview(content: str, width: int = 160) -> str:
    flat = " ".join(content.split())
    return flat[:width] + ("…" if len(flat) > width else "")
