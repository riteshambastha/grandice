"""Tests for §05. Each one corresponds to a failure mode a weaker model hits."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import Ledger, CostCapExceeded, Reply, Router, ToolCall, _parse_call
from grandice.session import build as build_session
from grandice.tools.base import Risk, ToolSpec, truncate, validate


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


# §05.1 — argument validation
async def test_invalid_arguments_return_a_usable_error(session):
    result = await agent_loop.execute(session, ToolCall("1", "read", {"file": "x.txt"}))
    assert result["content"].startswith("ERROR:")
    assert "path" in result["content"]  # names the parameter it actually wants


async def test_unknown_tool_lists_the_real_ones(session):
    result = await agent_loop.execute(session, ToolCall("1", "delete_everything", {}))
    assert "No tool" in result["content"]
    assert "glob" in result["content"]


def test_malformed_json_becomes_a_validation_error_not_a_crash():
    call = _parse_call({"id": "1", "name": "read", "args": '{"path": "a.txt"'})
    assert "__malformed__" in call.arguments
    ok, message = validate(call.arguments, {"type": "object", "properties": {"path": {}}})
    assert not ok and "not valid JSON" in message


# §05.2 — tool budget
def test_registry_refuses_to_exceed_the_active_tool_cap(session):
    noop = ToolSpec("x", "", {"type": "object"}, run=None)
    for i in range(20):
        session.registry.add(replace(noop, name=f"filler_{i}"))
    with pytest.raises(RuntimeError, match="cap is 15"):
        session.registry.active()


# §05.4 — truncation leaves a pointer
def test_truncation_writes_the_full_output_and_names_the_path(tmp_path):
    text = "\n".join(f"line {i}" for i in range(50_000))
    out = truncate(text, budget_tokens=200, overflow_dir=tmp_path, label="bash")
    assert len(out) < len(text)
    spilled = list(tmp_path.glob("bash-*.txt"))
    assert spilled and spilled[0].read_text() == text
    assert str(spilled[0]) in out  # the model is told where to look


# §05.7 — line-range edits with a staleness check
async def test_stale_edit_is_refused_and_shows_current_contents(session):
    target = session.sandbox.workspace / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\nbravo\ncharlie\n")

    result = await agent_loop.execute(
        session,
        ToolCall("1", "edit", {
            "path": "notes.md", "start_line": 2, "end_line": 2,
            "replacement": "BRAVO", "expected_hash": "deadbeef",
        }),
    )
    assert result["content"].startswith("STALE:")
    assert "bravo" in result["content"]           # shows what is actually there
    assert target.read_text() == "alpha\nbravo\ncharlie\n"  # unchanged


async def test_edit_replaces_the_range_and_echoes_the_result(session):
    target = session.sandbox.workspace / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\nbravo\ncharlie\n")

    result = await agent_loop.execute(
        session,
        ToolCall("1", "edit", {
            "path": "notes.md", "start_line": 2, "end_line": 2, "replacement": "BRAVO",
        }),
    )
    assert not result["content"].startswith(("ERROR:", "STALE:"))
    assert target.read_text() == "alpha\nBRAVO\ncharlie\n"


# §05.5 — the plan tool
async def test_todo_rejects_two_items_in_progress(session):
    result = await agent_loop.execute(
        session,
        ToolCall("1", "todo", {"items": [
            {"task": "a", "status": "in_progress"},
            {"task": "b", "status": "in_progress"},
        ]}),
    )
    assert "one at a time" in result["content"]
    assert session.todos.items == []


# §05.8 — reflection breaks retry loops
async def test_second_consecutive_failure_injects_a_rethink(session):
    events = []
    async for event in agent_loop.run_turn(session, "go"):
        events.append(event)

    for _ in range(2):
        await agent_loop.execute(session, ToolCall("1", "read", {"path": "missing.txt"}))
        session.note_failure("read")
    assert session.failures["read"] >= 2


# §06 — the sandbox boundary
async def test_paths_outside_the_workspace_are_refused(session):
    result = await agent_loop.execute(
        session, ToolCall("1", "read", {"path": "../../../etc/passwd"})
    )
    assert "outside the workspace" in result["content"]


async def test_shell_cannot_write_outside_the_workspace(session, tmp_path):
    victim = tmp_path / "victim.txt"
    await agent_loop.execute(
        session, ToolCall("1", "bash", {"command": f"echo pwned > {victim}"})
    )
    assert not victim.exists()


async def test_shell_has_no_network(session):
    result = await agent_loop.execute(
        session,
        ToolCall("1", "bash", {"command": "curl -s -m 3 https://example.com && echo REACHED"}),
    )
    assert "REACHED" not in result["content"]


async def test_runaway_command_is_killed(session):
    result = await agent_loop.execute(
        session, ToolCall("1", "bash", {"command": "sleep 30", "timeout": 2})
    )
    assert "wall-clock cap" in result["content"]


# §08 — the permission gate
async def test_outward_tools_stop_for_a_human(session):
    async def send(to: str) -> str:  # pragma: no cover - must never run
        raise AssertionError("outward tool ran without approval")

    session.registry.add(ToolSpec(
        name="send_email",
        description="",
        schema={"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]},
        run=send,
        risk=Risk.OUTWARD,
    ))
    result = await agent_loop.execute(session, ToolCall("1", "send_email", {"to": "a@b.c"}))
    assert "declined" in result["content"]


# §11 — the cost cap
def test_ledger_stops_the_session_at_the_cap():
    ledger = Ledger(cap_usd=0.01)
    ledger.record("orchestrator", prompt_tokens=5_000_000, completion_tokens=0)
    with pytest.raises(CostCapExceeded):
        ledger.check()


# §08.5 — the audit log
async def test_every_tool_call_is_logged_with_its_arguments(session):
    await agent_loop.execute(session, ToolCall("1", "glob", {"pattern": "**/*"}))
    records = [json.loads(l) for l in session.log_path.read_text().splitlines()]
    calls = [r for r in records if r["kind"] == "tool_call"]
    assert calls and calls[0]["arguments"] == {"pattern": "**/*"}
