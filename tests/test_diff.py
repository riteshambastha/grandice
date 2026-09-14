"""Tests for the file-diff capture added in P5 (§loop.py's FileChanged)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import ToolCall
from grandice.session import build as build_session


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


def test_snapshot_is_none_for_non_file_tools(session):
    call = ToolCall("1", "bash", {"command": "echo hi"})
    assert agent_loop._snapshot_if_relevant(session, call) is None


def test_snapshot_is_empty_string_for_a_new_file(session):
    call = ToolCall("1", "write", {"path": "new.txt", "content": "hi"})
    assert agent_loop._snapshot_if_relevant(session, call) == ""


def test_snapshot_captures_existing_content(session):
    (session.sandbox.workspace / "existing.txt").write_text("original\n")
    call = ToolCall("1", "edit", {"path": "existing.txt", "start_line": 1, "end_line": 1, "replacement": "x"})
    assert agent_loop._snapshot_if_relevant(session, call) == "original\n"


async def test_diff_event_shows_a_real_unified_diff(session):
    target = session.sandbox.workspace / "a.txt"
    target.write_text("line1\nline2\n")
    before = "line1\nline2\n"

    await agent_loop.execute(
        session, ToolCall("1", "edit", {"path": "a.txt", "start_line": 2, "end_line": 2, "replacement": "changed"})
    )
    event = agent_loop._diff_event(session, "a.txt", before)
    assert event.path == "a.txt"
    assert "-line2" in event.diff
    assert "+changed" in event.diff


async def test_diff_event_for_a_new_file_says_so(session):
    await agent_loop.execute(session, ToolCall("1", "write", {"path": "b.txt", "content": "brand new"}))
    event = agent_loop._diff_event(session, "b.txt", "")
    assert event.diff.startswith("(new file)")
    assert "brand new" in event.diff


async def test_diff_event_with_no_real_change_says_so(session):
    target = session.sandbox.workspace / "c.txt"
    target.write_text("same\n")
    event = agent_loop._diff_event(session, "c.txt", "same\n")
    assert event.diff == "(no textual difference)"


async def test_run_turn_yields_file_changed_after_a_successful_write(session):
    from grandice.router import Reply, StubBackend

    class WriteOnceStub(StubBackend):
        def __init__(self):
            self._n = 0

        async def complete(self, model, messages, tools, temperature, max_tokens=None):
            self._n += 1
            if self._n == 1:
                yield Reply(text="", tool_calls=[ToolCall("1", "write", {"path": "note.txt", "content": "hi"})])
            else:
                yield Reply(text="done")

    session.router.backends = [WriteOnceStub()]
    events = [e async for e in agent_loop.run_turn(session, "write a note")]
    changes = [e for e in events if isinstance(e, agent_loop.FileChanged)]
    assert len(changes) == 1
    assert changes[0].path == "note.txt"
    assert changes[0].diff == "(new file)\nhi"


async def test_run_turn_does_not_diff_a_failed_write(session):
    """A write that fails validation shouldn't produce a misleading diff of
    nothing having changed."""
    from grandice.router import Reply, StubBackend

    class BadWriteStub(StubBackend):
        def __init__(self):
            self._n = 0

        async def complete(self, model, messages, tools, temperature, max_tokens=None):
            self._n += 1
            if self._n == 1:
                # missing required "content" argument -> validation error
                yield Reply(text="", tool_calls=[ToolCall("1", "write", {"path": "note.txt"})])
            else:
                yield Reply(text="done")

    session.router.backends = [BadWriteStub()]
    events = [e async for e in agent_loop.run_turn(session, "write a note")]
    assert not any(isinstance(e, agent_loop.FileChanged) for e in events)
