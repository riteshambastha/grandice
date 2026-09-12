"""Tests for subagent isolation (§P4): the hard exclusions (§08's "no
outward-facing capability at all", no nested spawning), shared router/
sandbox, and a real run through the stub backend."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import ToolCall
from grandice.session import build as build_session
from grandice.subagents import build_child_session, run_subagent
from grandice.tools.base import Risk, ToolSpec


@pytest.fixture
def session(tmp_path: Path):
    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
    )
    return build_session(config, Gate(always_deny))


# --- registry restriction ---------------------------------------------------

def test_child_never_gets_the_recursive_tools(session):
    child = build_child_session(session)
    names = set(child.registry.names())
    assert names.isdisjoint({"spawn_subagent", "spawn_background", "check_task", "list_tasks"})


def test_child_never_gets_an_outward_tool_even_if_explicitly_requested(session):
    async def fake_fetch(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("an outward tool ran inside a subagent")

    session.registry.add(
        ToolSpec(name="fetch.fetch", description="", schema={"type": "object"},
                 run=fake_fetch, risk=Risk.OUTWARD),
        active=False,
    )
    child = build_child_session(session, tools=["fetch.fetch", "read"])
    assert "fetch.fetch" not in child.registry.names()
    assert "read" in child.registry.names()  # the rest of the explicit request still works


def test_explicit_tools_list_narrows_to_only_those(session):
    child = build_child_session(session, tools=["read", "glob"])
    assert set(child.registry.names()) == {"read", "glob"}


def test_default_tools_is_everything_the_parent_has_minus_exclusions(session):
    child = build_child_session(session)
    expected = set(session.registry.names()) - {
        "spawn_subagent", "spawn_background", "check_task", "list_tasks",
    }
    assert set(child.registry.names()) == expected


def test_child_shares_router_and_sandbox_not_registry_or_todos(session):
    child = build_child_session(session)
    assert child.router is session.router
    assert child.sandbox is session.sandbox
    assert child.registry is not session.registry
    assert child.todos is not session.todos
    assert child.todos.items == []  # a fresh plan, not the parent's


def test_child_gets_a_smaller_step_cap(session):
    child = build_child_session(session)
    assert child.config.max_steps == session.config.subagent_max_steps
    assert child.config.max_steps < session.config.max_steps


# --- run_subagent, end to end through the stub backend ----------------------

async def test_run_subagent_returns_a_report_with_a_footer(session):
    report = await run_subagent(session, "list the workspace")
    assert "[subagent:" in report
    assert "steps" in report


async def test_run_subagent_updates_the_shared_ledger(session):
    before = session.router.ledger.calls
    await run_subagent(session, "list the workspace")
    assert session.router.ledger.calls > before  # cost aggregates on the parent, not lost in the child


async def test_run_subagent_runs_on_the_worker_tier(session, monkeypatch):
    seen_tiers = []
    original = session.router.complete

    def spy(tier, *a, **kw):
        seen_tiers.append(tier)
        return original(tier, *a, **kw)

    monkeypatch.setattr(session.router, "complete", spy)
    await run_subagent(session, "list the workspace")
    assert seen_tiers and all(t == "worker" for t in seen_tiers)


# --- the tools themselves, through agent_loop.execute -----------------------

async def test_spawn_subagent_tool_dispatches_correctly(session):
    result = await agent_loop.execute(session, ToolCall("1", "spawn_subagent", {"task": "list the workspace"}))
    assert not result["content"].startswith("ERROR:")
    assert "[subagent:" in result["content"]


async def test_spawn_background_then_check_task_round_trips(session):
    started = await agent_loop.execute(
        session, ToolCall("1", "spawn_background", {"task": "list the workspace"})
    )
    assert "Started background task" in started["content"]
    task_id = started["content"].split("'")[1]

    # Stub backend resolves near-instantly; give the scheduled task a moment.
    for _ in range(50):
        task = session.tasks.get(task_id)
        if task.status.value != "pending" and task.status.value != "running":
            break
        await asyncio.sleep(0.02)

    checked = await agent_loop.execute(session, ToolCall("1", "check_task", {"task_id": task_id}))
    assert checked["content"].startswith("[done]")


async def test_check_task_on_unknown_id_is_a_clean_error(session):
    result = await agent_loop.execute(session, ToolCall("1", "check_task", {"task_id": "nonexistent"}))
    assert result["content"].startswith("ERROR:")
    assert "list_tasks" in result["content"]


async def test_list_tasks_reports_none_when_empty(session):
    result = await agent_loop.execute(session, ToolCall("1", "list_tasks", {}))
    assert "No background tasks" in result["content"]
