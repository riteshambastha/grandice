"""Tests for the live-view dashboard (FastAPI + SSE). The stub backend
resolves in well under a millisecond, which makes it useless for actually
racing the /api/task concurrency guard over real HTTP — so that guard is
tested directly against AppState instead of by trying to win a timing race."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.server import app as server_app
from grandice.server.broadcast import Broadcaster
from grandice.server.serialize import event_to_dict
from grandice.session import build as build_session
from grandice import loop as agent_loop


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


@pytest.fixture
def client(session):
    app = server_app.create_app(session=session)
    with TestClient(app) as c:
        yield c


# --- serialize.py ---------------------------------------------------------

def test_event_to_dict_covers_every_event_type():
    assert event_to_dict(agent_loop.TextDelta("hi")) == {"type": "text_delta", "text": "hi"}
    assert event_to_dict(agent_loop.ToolStarted("glob", {"pattern": "*"})) == {
        "type": "tool_started", "name": "glob", "arguments": {"pattern": "*"},
    }
    assert event_to_dict(agent_loop.ToolFinished("glob", True, "ok")) == {
        "type": "tool_finished", "name": "glob", "ok": True, "preview": "ok",
    }
    assert event_to_dict(agent_loop.FileChanged("a.txt", "--- a/a.txt\n+++ b/a.txt\n")) == {
        "type": "file_changed", "path": "a.txt", "diff": "--- a/a.txt\n+++ b/a.txt\n",
    }
    assert event_to_dict(agent_loop.Finished("done", 3, 0.01)) == {
        "type": "finished", "reason": "done", "steps": 3, "cost_usd": 0.01,
    }


# --- broadcast.py -----------------------------------------------------------

def test_broadcaster_delivers_to_subscribers_and_keeps_bounded_history():
    b = Broadcaster(history_limit=2)
    q = b.subscribe()
    b.publish({"n": 1})
    b.publish({"n": 2})
    b.publish({"n": 3})  # pushes {"n": 1} out of history

    delivered = []
    while not q.empty():
        delivered.append(q.get_nowait())

    assert delivered == [{"n": 1}, {"n": 2}, {"n": 3}]  # a subscriber sees everything published
    assert b.history == [{"n": 2}, {"n": 3}]  # history itself stays bounded


def test_unsubscribe_stops_delivery():
    b = Broadcaster()
    q = b.subscribe()
    b.unsubscribe(q)
    b.publish({"n": 1})
    assert q.empty()


# --- HTTP endpoints ---------------------------------------------------------

def test_index_serves_the_dashboard(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "grandice" in res.text.lower()


def test_state_reports_model_sandbox_and_zero_cost(client, session):
    res = client.get("/api/state")
    body = res.json()
    assert body["sandbox"] == "sandbox-exec"
    assert body["live"] is False
    assert body["running"] is False
    assert body["cost"]["spent_usd"] == 0.0
    assert body["rate_limit"] is None  # stub mode: nothing to pace
    assert {s["name"] for s in body["skills"]} == {"xlsx", "pptx", "docx", "pdf"}
    assert body["tasks"] == []


def test_state_reports_background_tasks(client, session):
    task_id = session.tasks.create("subagent", "investigate something")
    session.tasks.mark_done(task_id, "found it")

    body = client.get("/api/state").json()
    assert body["tasks"] == [
        {"id": task_id, "status": "done", "description": "investigate something",
         "result": "found it", "error": None}
    ]


def test_files_lists_the_workspace_root(client, session):
    (session.sandbox.workspace / "note.txt").write_text("hello")
    res = client.get("/api/files", params={"path": "."})
    names = {e["name"] for e in res.json()["entries"]}
    assert "note.txt" in names


def test_files_rejects_path_traversal(client):
    res = client.get("/api/files", params={"path": "../../../etc"})
    assert res.status_code == 403


def test_file_content_reads_a_real_file(client, session):
    (session.sandbox.workspace / "note.txt").write_text("hello there")
    res = client.get("/api/file_content", params={"path": "note.txt"})
    body = res.json()
    assert body["content"] == "hello there"
    assert body["binary"] is False


def test_file_content_rejects_path_traversal(client):
    res = client.get("/api/file_content", params={"path": "../../../etc/passwd"})
    assert res.status_code == 403


def test_file_content_404s_on_a_missing_file(client):
    res = client.get("/api/file_content", params={"path": "nope.txt"})
    assert res.status_code == 404


def test_task_rejects_an_empty_message(client):
    res = client.post("/api/task", json={"message": "   "})
    assert res.status_code == 400


def test_cancel_with_nothing_running_is_a_conflict(client):
    res = client.post("/api/cancel")
    assert res.status_code == 409


# --- the concurrency guard, tested directly rather than raced over HTTP ----

def test_task_is_refused_while_one_is_already_running(client, session):
    state = client.app.state.grandice
    state.running = True  # simulate a task already in flight
    res = client.post("/api/task", json={"message": "another one"})
    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


# --- a real run end to end, through the stub backend ------------------------

def test_a_real_task_streams_events_and_updates_state(client, session):
    res = client.post("/api/task", json={"message": "list the workspace"})
    assert res.status_code == 202

    # The stub backend resolves near-instantly; TestClient runs the server
    # in-process, so by the time this returns the background task has had
    # its chance to run to completion.
    import time
    for _ in range(50):
        if not client.get("/api/state").json()["running"]:
            break
        time.sleep(0.02)

    log = client.get("/api/log").json()
    types = [e["type"] for e in log]
    assert "tool_started" in types
    assert "tool_finished" in types
    assert "finished" in types
    assert types[-1] == "state_changed"  # _run's finally publishes this last, to prompt a refetch

    state = client.get("/api/state").json()
    assert state["running"] is False
    assert state["cost"]["calls"] > 0


# --- MCP connectors through the dashboard's lifespan (not the CLI's) ------

def test_lifespan_starts_and_stops_a_configured_connector(tmp_path):
    """The dashboard starts connectors via a FastAPI lifespan handler — a
    different code path from the CLI's asyncio.run wrapper (see
    server/app.py's create_app). Skipped if the optional `mcp` extras
    aren't installed."""
    pytest.importorskip("mcp")
    pytest.importorskip("mcp_server_sqlite")

    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
        mcp_connectors=("sqlite",),
    )
    session = build_session(config, Gate(always_deny))

    with TestClient(server_app.create_app(session=session)) as c:
        state = c.get("/api/state").json()
        assert state["connectors"] == ["sqlite"]
        assert "sqlite.read_query" in state["tools"]["latent"]
        assert not any(n.startswith("sqlite.") for n in state["tools"]["active"])
    # __exit__ triggers the lifespan's shutdown half; a hung or raising
    # stop_connectors would surface as this test failing to complete.


# --- in-browser approvals (§P5) --------------------------------------------

def _build_app_with_outward_tool(tmp_path: Path, monkeypatch) -> tuple:
    """create_app() with no session builds its own using web_ask as the
    Asker — this is the path that actually needs testing, so unlike other
    tests here we don't pass a pre-built session."""
    monkeypatch.setenv("GRANDICE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("GRANDICE_API_KEY", raising=False)
    monkeypatch.delenv("GRANDICE_BASE_URL", raising=False)

    app = server_app.create_app()
    state = app.state.grandice

    from grandice.tools.base import Risk, ToolSpec

    async def fake_send(**kwargs):
        return "sent"

    state.session.registry.add(
        ToolSpec(
            name="send_email",
            description="",
            schema={"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]},
            run=fake_send,
            risk=Risk.OUTWARD,
        )
    )

    from grandice.router import Reply, StubBackend
    from grandice.router import ToolCall as RC

    class OutwardOnceStub(StubBackend):
        def __init__(self) -> None:
            self._n = 0

        async def complete(self, model, messages, tools, temperature):
            self._n += 1
            if self._n == 1:
                yield Reply(text="", tool_calls=[RC("1", "send_email", {"to": "a@b.c"})])
            else:
                yield Reply(text="done")

    state.session.router.backends = [OutwardOnceStub()]
    return app, state


def _wait_for_approval_request(client) -> str:
    import time

    for _ in range(50):
        log = client.get("/api/log").json()
        approvals = [e for e in log if e["type"] == "approval_needed"]
        if approvals:
            return approvals[0]["request_id"]
        time.sleep(0.02)
    raise AssertionError("no approval_needed event appeared in time")


def _wait_until_not_running(client) -> None:
    import time

    for _ in range(50):
        if not client.get("/api/state").json()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("task never finished")


def test_approving_lets_the_outward_tool_run(tmp_path, monkeypatch):
    app, _ = _build_app_with_outward_tool(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post("/api/task", json={"message": "send an email"})
        request_id = _wait_for_approval_request(c)

        res = c.post("/api/approve", json={"request_id": request_id, "approved": True})
        assert res.status_code == 200

        _wait_until_not_running(c)
        log = c.get("/api/log").json()
        finishes = [e for e in log if e["type"] == "tool_finished" and e["name"] == "send_email"]
        assert finishes and finishes[0]["ok"] is True


def test_denying_stops_the_outward_tool(tmp_path, monkeypatch):
    app, _ = _build_app_with_outward_tool(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post("/api/task", json={"message": "send an email"})
        request_id = _wait_for_approval_request(c)

        c.post("/api/approve", json={"request_id": request_id, "approved": False})

        _wait_until_not_running(c)
        log = c.get("/api/log").json()
        finishes = [e for e in log if e["type"] == "tool_finished" and e["name"] == "send_email"]
        assert finishes and finishes[0]["ok"] is False
        assert "declined" in finishes[0]["preview"].lower()


def test_approve_404s_on_an_unknown_request_id(client):
    res = client.post("/api/approve", json={"request_id": "nonexistent", "approved": True})
    assert res.status_code == 404
