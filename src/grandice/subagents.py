"""Subagents (§P4): an isolated context for one bounded piece of work.

The point is keeping a side-investigation out of the orchestrator's own
context — a subagent runs its own turn to completion with its own message
history and plan, and only its final report comes back, not every step of
how it got there. Runs on the "worker" tier by default (§03: a real model
tier in the roster, not invented here) — bounded, mechanical work doesn't
need the orchestrator's reasoning budget.

Per §08 ("Split reading from acting... a tool set with no outward-facing
capability at all"), a subagent never gets an outward tool, and never gets
the spawn tools themselves — no nested subagents in this build. Both rules
are enforced here, not left to the caller's judgment.
"""

from __future__ import annotations

from dataclasses import replace

from . import loop as agent_loop
from .permissions import Gate, always_deny
from .session import Session
from .tools import Registry
from .tools.base import Risk
from .tools.todo import TodoList

# Tools a subagent must never receive, regardless of what's requested — a
# hard rule, not a caller-configurable default. Prevents unbounded recursive
# spawning; a flat one-level hierarchy is the deliberate scope here.
_RECURSIVE_TOOL_NAMES = {"spawn_subagent", "spawn_background", "check_task", "list_tasks"}


def _restricted_registry(parent: Session, requested: list[str] | None) -> Registry:
    """A fresh registry for the child. Defaults to everything the parent has
    (active or latent) minus the hard exclusions below; an explicit
    `requested` list narrows further but can never add back what's excluded."""
    child = Registry()
    available = {s.name for s in parent.registry.active()} | {s.name for s in parent.registry.latent()}
    names = (set(requested) & available) if requested is not None else available
    names -= _RECURSIVE_TOOL_NAMES

    for name in sorted(names):
        spec = parent.registry.get(name)
        if spec is None or spec.risk is Risk.OUTWARD:
            continue  # OUTWARD exclusion is the hard rule from §08, not optional
        child.add(spec)
    return child


def build_child_session(parent: Session, tools: list[str] | None = None) -> Session:
    """Isolated context, shared everything else that needs to stay shared:
    the Router (so the cost ledger and rate limiter aggregate correctly
    across parent + subagent — a fresh Router would silently reset the
    daily cap for every subagent spawned) and the Sandbox (same workspace;
    files are the medium subagents actually report through)."""
    registry = _restricted_registry(parent, tools)
    child_config = replace(parent.config, max_steps=parent.config.subagent_max_steps)
    return Session(
        config=child_config,
        router=parent.router,
        sandbox=parent.sandbox,
        registry=registry,
        gate=Gate(always_deny),  # a subagent never approves its own outward actions
        todos=TodoList(),
        skills=parent.skills,
        connectors=[],  # already flattened into the registry above; nothing left to start
        tasks=parent.tasks,
    )


async def run_subagent(parent: Session, task: str, tools: list[str] | None = None) -> str:
    """Run one subagent turn to completion and return a compact report —
    the child's own transcript stays in its own session.log, never in the
    parent's context."""
    child = build_child_session(parent, tools=tools)

    text_parts: list[str] = []
    finished: agent_loop.Finished | None = None
    async for event in agent_loop.run_turn(child, task, tier="worker"):
        if isinstance(event, agent_loop.TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, agent_loop.Finished):
            finished = event

    report = "".join(text_parts).strip() or "(subagent produced no text — see its own session log)"
    if finished is not None:
        report += f"\n\n[subagent: {finished.reason} · {finished.steps} steps · ~${finished.cost_usd:.3f}]"
    return report
