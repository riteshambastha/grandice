"""Tests for §05. Each one corresponds to a failure mode a weaker model hits."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import Backend, Ledger, CostCapExceeded, Reply, Router, ToolCall, _parse_call
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


# §05 — a genuinely empty reply from the model (no text, no tool calls)
#
# Real, observed failure mode on a self-hosted model (confirmed live against
# an Ollama-hosted qwen3:8b through the actual dashboard): a turn would end
# with no text and no tool call at all, and the loop treated that exactly
# like "done talking" with nothing to distinguish it — the dashboard/CLI
# showed a bare "done" line with zero visible content for that entire turn,
# indistinguishable from the model choosing to say nothing on purpose.

class _StubReplyBackend(Backend):
    """A backend that always yields one fixed Reply, for exercising
    run_turn's handling of that reply's shape."""

    name = "stub-reply"

    def __init__(self, reply: Reply) -> None:
        self._reply = reply

    async def complete(self, model, messages, tools, temperature, max_tokens=None):
        yield self._reply


async def test_empty_reply_gets_a_distinct_reason_not_a_silent_done(session):
    session.router.backends = [_StubReplyBackend(Reply(text="", tool_calls=[]))]

    events = [e async for e in agent_loop.run_turn(session, "hello")]

    text_deltas = [e for e in events if isinstance(e, agent_loop.TextDelta)]
    assert text_deltas == []  # nothing to stream — the reply was genuinely empty

    finished = [e for e in events if isinstance(e, agent_loop.Finished)][0]
    assert finished.reason != "done"
    assert "empty reply" in finished.reason


async def test_a_normal_text_only_reply_still_says_done(session):
    """The fix must not misfire on the ordinary "model is done talking"
    case — only a reply with *neither* text nor tool calls gets the new
    reason; ordinary final text still just says "done"."""
    session.router.backends = [_StubReplyBackend(Reply(text="All done.", tool_calls=[]))]

    events = [e async for e in agent_loop.run_turn(session, "hello")]
    finished = [e for e in events if isinstance(e, agent_loop.Finished)][0]
    assert finished.reason == "done"


# Real bug, found live right after the fix above shipped: flagging an empty
# reply is useless if the reply itself then poisons the transcript. Reply.
# message used to store `content: None` whenever there was no tool_calls —
# including this exact empty case — producing a `{"role": "assistant",
# "content": None}` turn with neither content nor a tool call. Sent back to
# a self-hosted model (confirmed live on Ollama-served qwen3:8b) as history,
# THAT degenerate turn broke its chat template outright: every following
# completion in the same chat also came back empty, in ~200ms, no matter
# what the user sent next — "try rephrasing, or send another message" could
# never actually recover the conversation. The fix is in Reply.message
# itself (router.py): fall back to "" instead of None when there's no
# tool_calls, so the persisted turn is always well-formed.
async def test_empty_reply_does_not_poison_the_transcript_with_a_null_turn(session):
    session.router.backends = [_StubReplyBackend(Reply(text="", tool_calls=[]))]

    events = [e async for e in agent_loop.run_turn(session, "hello")]
    assert [e for e in events if isinstance(e, agent_loop.Finished)]

    assistant_turn = session.messages[-1]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["content"] == ""  # well-formed — NOT None
    assert "tool_calls" not in assistant_turn


# §05.8 — the OTHER retry-loop shape: a call that keeps SUCCEEDING with the
# same arguments but never leads anywhere. Real bug, found live: asked to
# visualize an uploaded CSV, the model cycled todo -> glob -> read (same
# file, same limit) -> todo -> glob -> read... for the full 120-step budget
# (~$0.46) without ever attempting to actually produce a chart. Nothing
# there ever failed, so REFLECT_AFTER's failure-counting never fired.
class _RepeatingCallStub(Backend):
    """Yields the SAME tool call over and over, with one different call
    (`todo`) interleaved every other step — matching the exact cyclic
    shape of the real stuck loop, not just immediate back-to-back repeats."""

    name = "stub-repeating"

    def __init__(self, repeats: int) -> None:
        self._repeats = repeats
        self._n = 0

    async def complete(self, model, messages, tools, temperature, max_tokens=None):
        self._n += 1
        if self._n > self._repeats:
            yield Reply(text="Giving up — no new approach.")
            return
        if self._n % 2 == 1:
            yield Reply(text="", tool_calls=[ToolCall(str(self._n), "glob", {"pattern": "**/*.csv"})])
        else:
            # Varies each time (unlike the glob above) so ITS repeat count
            # never crosses the threshold first — this test is about the
            # glob repeating, not the todo.
            status = "in_progress" if self._n % 4 == 0 else "pending"
            yield Reply(text="", tool_calls=[ToolCall(str(self._n), "todo", {"items": [{"task": "look", "status": status}]})])


async def test_repeated_identical_successful_call_triggers_a_rethink(session):
    session.router.backends = [_RepeatingCallStub(repeats=6)]

    events = [e async for e in agent_loop.run_turn(session, "visualize this data")]
    assert [e for e in events if isinstance(e, agent_loop.Finished)]

    notes = [
        m for m in session.messages
        if m.get("role") == "user" and isinstance(m.get("content"), str) and "<system_note>" in m["content"]
    ]
    assert notes, "expected a repetition rethink to be injected into the transcript"
    assert "glob" in notes[0]["content"]


async def test_a_couple_of_repeats_do_not_yet_trigger_a_rethink(session):
    """Must not misfire on ordinary, brief re-checking — only genuinely
    excessive repetition (REPEAT_REFLECT_AFTER) should trigger this."""
    session.router.backends = [_RepeatingCallStub(repeats=2)]

    events = [e async for e in agent_loop.run_turn(session, "visualize this data")]
    assert [e for e in events if isinstance(e, agent_loop.Finished)]

    notes = [
        m for m in session.messages
        if m.get("role") == "user" and isinstance(m.get("content"), str) and "<system_note>" in m["content"]
    ]
    assert notes == []
