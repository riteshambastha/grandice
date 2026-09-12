"""spawn_subagent / spawn_background / check_task / list_tasks (§P4).

A subagent gets an isolated context — its own message history, its own
plan — so a side-investigation doesn't bloat the orchestrator's window with
every step of how it got there; only the final report comes back. The same
"keep noise out of context" instinct as truncation (§05.4), applied to a
whole sub-conversation instead of one tool result.

`spawn_subagent` blocks until the subagent finishes — like calling a
function. `spawn_background` returns immediately with a task id, for work
long enough that the orchestrator would rather keep going and check back
later via `check_task`/`list_tasks`.
"""

from __future__ import annotations

import asyncio

from ..subagents import run_subagent
from ..tasks import TaskStatus
from .base import Risk, ToolSpec

_TOOLS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional: exact tool names to give the subagent (see your own active/latent "
        "tools). Omit to hand it everything you have — outward-facing tools (fetch, "
        "approvals) and the spawn/task tools themselves are never included either way."
    ),
}


def build(session) -> list[ToolSpec]:
    async def spawn_subagent(task: str, tools: list[str] | None = None) -> str:
        return await run_subagent(session, task, tools=tools)

    async def spawn_background(task: str, tools: list[str] | None = None) -> str:
        task_id = session.tasks.create(kind="subagent", description=task)

        async def _run() -> None:
            session.tasks.mark_running(task_id)
            try:
                report = await run_subagent(session, task, tools=tools)
                session.tasks.mark_done(task_id, report)
            except Exception as exc:  # noqa: BLE001 — a crashed background task must not vanish silently
                session.tasks.mark_failed(task_id, f"{type(exc).__name__}: {exc}")

        asyncio.create_task(_run())
        return f"Started background task {task_id!r}. Check on it with check_task({task_id!r})."

    async def check_task(task_id: str) -> str:
        task = session.tasks.get(task_id)
        if task is None:
            raise ValueError(f"No task {task_id!r}. Use list_tasks to see what actually exists.")
        if task.status is TaskStatus.DONE:
            return f"[done] {task.result}"
        if task.status is TaskStatus.FAILED:
            return f"[failed] {task.error}"
        if task.status is TaskStatus.INTERRUPTED:
            return (
                "[interrupted] The process restarted while this was running, so it never "
                "finished. Its result was lost — spawn_background the same task again to retry."
            )
        return f"[{task.status.value}] still working — check again in a moment."

    async def list_tasks() -> str:
        tasks = session.tasks.list()
        if not tasks:
            return "No background tasks yet."
        return "\n".join(
            f"{t.id} [{t.status.value}] {t.description[:70]}" for t in tasks
        )

    return [
        ToolSpec(
            name="spawn_subagent",
            description=(
                "Run a bounded sub-task in its own isolated context and get back only its "
                "final report — use this for a side-investigation you don't want cluttering "
                "your own conversation (e.g. 'read these 12 files and summarise what changed'). "
                "Blocks until the subagent finishes."
            ),
            schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What the subagent should do."},
                    "tools": _TOOLS_SCHEMA,
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            run=spawn_subagent,
            risk=Risk.WRITE,
        ),
        ToolSpec(
            name="spawn_background",
            description=(
                "Like spawn_subagent, but returns immediately with a task id instead of "
                "waiting — use this for a task long enough that you'd rather keep working "
                "and check back later with check_task."
            ),
            schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What the subagent should do."},
                    "tools": _TOOLS_SCHEMA,
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            run=spawn_background,
            risk=Risk.WRITE,
        ),
        ToolSpec(
            name="check_task",
            description="Check a background task's status, started by spawn_background.",
            schema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            run=check_task,
            risk=Risk.READ,
        ),
        ToolSpec(
            name="list_tasks",
            description="List every background task, current and past, for this workspace.",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            run=list_tasks,
            risk=Risk.READ,
        ),
    ]
