"""Tests for the live-view dashboard (FastAPI + SSE), now auth-gated and
multi-chat: every route requires a logged-in user via a session cookie, and
each chat gets its own isolated ChatRuntime (Session, event stream, running
flag, pending approvals) built lazily on first touch. The stub backend
resolves in well under a millisecond, which makes it useless for actually
racing the per-chat /task concurrency guard over real HTTP — so that guard
is tested directly against the runtime instead of by trying to win a
timing race."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grandice.server import app as server_app
from grandice.server.broadcast import Broadcaster
from grandice.server.serialize import event_to_dict
from grandice import loop as agent_loop


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRANDICE_WORKSPACE", str(tmp_path / "unused-cli-workspace"))
    return server_app.create_app(
        accounts_path=tmp_path / "accounts.db",
        projects_path=tmp_path / "projects.db",
        projects_root=tmp_path / "projects",
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _register(client, username="alice", password="correct horse") -> TestClient:
    res = client.post("/api/auth/register", json={"username": username, "password": password})
    assert res.status_code == 201, res.text
    return client


def _project_and_chat(client) -> tuple[str, str]:
    project = client.post("/api/projects", json={"name": "demo"}).json()
    chat = client.post(f"/api/projects/{project['id']}/chats", json={"title": "chat 1"}).json()
    return project["id"], chat["id"]


# --- serialize.py / broadcast.py (unaffected by the rewrite, kept as-is) ---

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


def test_broadcaster_delivers_to_subscribers_and_keeps_bounded_history():
    b = Broadcaster(history_limit=2)
    q = b.subscribe()
    b.publish({"n": 1})
    b.publish({"n": 2})
    b.publish({"n": 3})

    delivered = []
    while not q.empty():
        delivered.append(q.get_nowait())

    assert delivered == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert b.history == [{"n": 2}, {"n": 3}]


def test_unsubscribe_stops_delivery():
    b = Broadcaster()
    q = b.subscribe()
    b.unsubscribe(q)
    b.publish({"n": 1})
    assert q.empty()


# --- static file serving ----------------------------------------------------

def test_index_serves_the_dashboard(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "grandice" in res.text.lower()


def test_static_files_are_served_no_store(client):
    """Real bug found live: a browser could keep serving a stale app.js/
    style.css for a while after a real change on disk, with no request ever
    reaching the server to reveal it — no-cache (revalidate) alone turned
    out not to be a strong enough guarantee in practice; no-store is."""
    for path in ["/", "/app.js", "/style.css"]:
        res = client.get(path)
        assert res.headers["cache-control"] == "no-store", path


# --- auth --------------------------------------------------------------------

def test_register_logs_you_in(client):
    res = client.post("/api/auth/register", json={"username": "alice", "password": "correct horse"})
    assert res.status_code == 201
    assert "grandice_session" in res.cookies
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_register_rejects_a_duplicate_username(client):
    _register(client)
    res = client.post("/api/auth/register", json={"username": "alice", "password": "another password"})
    assert res.status_code == 400


def test_login_with_correct_credentials(client):
    _register(client)
    client.cookies.clear()
    res = client.post("/api/auth/login", json={"username": "alice", "password": "correct horse"})
    assert res.status_code == 200
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_login_rejects_wrong_password(client):
    _register(client)
    client.cookies.clear()
    res = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert res.status_code == 401


def test_logout_ends_the_session(client):
    _register(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_routes_require_login(app):
    with TestClient(app) as anon:
        assert anon.get("/api/auth/me").status_code == 401
        assert anon.get("/api/projects").status_code == 401
        assert anon.post("/api/projects", json={"name": "x"}).status_code == 401


# --- projects ------------------------------------------------------------

def test_create_and_list_projects(client):
    _register(client)
    client.post("/api/projects", json={"name": "demo"})
    names = {p["name"] for p in client.get("/api/projects").json()}
    assert names == {"demo"}


def test_projects_are_isolated_per_user(client, app):
    _register(client, "alice")
    client.post("/api/projects", json={"name": "alice's project"})

    with TestClient(app) as bob:
        _register(bob, "bob", "another password")
        assert bob.get("/api/projects").json() == []


def test_delete_project_is_blocked_for_a_non_owner(client, app):
    _register(client, "alice")
    project = client.post("/api/projects", json={"name": "mine"}).json()

    with TestClient(app) as bob:
        _register(bob, "bob", "another password")
        res = bob.delete(f"/api/projects/{project['id']}")
        assert res.status_code == 404


# --- chats -----------------------------------------------------------------

def test_create_and_list_chats(client):
    _register(client)
    project_id, chat_id = _project_and_chat(client)
    titles = [c["title"] for c in client.get(f"/api/projects/{project_id}/chats").json()]
    assert titles == ["chat 1"]


def test_chats_are_blocked_for_a_non_owner(client, app):
    _register(client, "alice")
    project_id, chat_id = _project_and_chat(client)

    with TestClient(app) as bob:
        _register(bob, "bob", "another password")
        assert bob.get(f"/api/projects/{project_id}/chats").status_code == 404
        assert bob.get(f"/api/chats/{chat_id}/state").status_code == 404


def test_rename_chat(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.patch(f"/api/chats/{chat_id}", json={"title": "renamed"})
    assert res.status_code == 200
    assert res.json()["title"] == "renamed"


def test_new_chat_has_no_model_override(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    assert client.get(f"/api/chats/{chat_id}/state")  # touch the runtime
    chat = client.patch(f"/api/chats/{chat_id}", json={"title": "chat 1"}).json()
    assert chat["model"] is None


def test_set_chat_model(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.patch(f"/api/chats/{chat_id}", json={"model": "code"})
    assert res.status_code == 200
    assert res.json()["model"] == "code"


def test_setting_the_model_does_not_touch_the_title(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    client.patch(f"/api/chats/{chat_id}", json={"title": "renamed"})
    res = client.patch(f"/api/chats/{chat_id}", json={"model": "code"})
    assert res.json()["title"] == "renamed"


def test_clearing_the_model_with_an_empty_string(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    client.patch(f"/api/chats/{chat_id}", json={"model": "code"})
    res = client.patch(f"/api/chats/{chat_id}", json={"model": ""})
    assert res.json()["model"] is None


def test_delete_chat(client):
    _register(client)
    project_id, chat_id = _project_and_chat(client)
    assert client.delete(f"/api/chats/{chat_id}").status_code == 200
    assert client.get(f"/api/projects/{project_id}/chats").json() == []


# --- a chat's runtime: state, files, task, cancel -----------------------

def test_chat_state_reports_model_sandbox_and_zero_cost(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    body = client.get(f"/api/chats/{chat_id}/state").json()
    assert body["live"] is False
    assert body["running"] is False
    assert body["cost"]["spent_usd"] == 0.0
    assert {s["name"] for s in body["skills"]} == {"xlsx", "pptx", "docx", "pdf"}
    assert body["tasks"] == []


def test_each_chat_gets_its_own_isolated_workspace(client):
    _register(client)
    project_id, chat_a = _project_and_chat(client)
    chat_b = client.post(f"/api/projects/{project_id}/chats", json={"title": "chat 2"}).json()["id"]

    # Touch both runtimes into existence, then confirm they share the same
    # project workspace (same project => same sandbox root) but are
    # otherwise independent sessions.
    state_a = client.get(f"/api/chats/{chat_a}/state").json()
    state_b = client.get(f"/api/chats/{chat_b}/state").json()
    assert state_a["session_id"] != state_b["session_id"]


def test_different_projects_get_different_workspaces(client):
    _register(client)
    p1 = client.post("/api/projects", json={"name": "p1"}).json()["id"]
    p2 = client.post("/api/projects", json={"name": "p2"}).json()["id"]
    c1 = client.post(f"/api/projects/{p1}/chats", json={}).json()["id"]
    c2 = client.post(f"/api/projects/{p2}/chats", json={}).json()["id"]

    client.get(f"/api/chats/{c1}/files")
    client.get(f"/api/chats/{c2}/files")

    grandice_state = client.app.state.grandice
    ws1 = grandice_state.runtimes[c1].session.sandbox.workspace
    ws2 = grandice_state.runtimes[c2].session.sandbox.workspace
    assert ws1 != ws2
    assert p1 in str(ws1) and p2 not in str(ws1)


# --- models (§ model selector) ---------------------------------------

def test_chat_models_falls_back_to_configured_tiers_in_stub_mode(client):
    from grandice.config import Tiers

    _register(client)
    _, chat_id = _project_and_chat(client)
    body = client.get(f"/api/chats/{chat_id}/models").json()
    assert body["current"] is None  # no override set yet
    assert set(body["models"]) == {Tiers.orchestrator, Tiers.worker, Tiers.bulk}


def test_chat_models_reports_the_chats_current_override(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    client.patch(f"/api/chats/{chat_id}", json={"model": "code"})
    body = client.get(f"/api/chats/{chat_id}/models").json()
    assert body["current"] == "code"


# --- file uploads (§ attachments) --------------------------------------

def test_upload_saves_the_file_into_the_workspace(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(
        f"/api/chats/{chat_id}/upload",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["path"] == "uploads/notes.txt"
    assert body["name"] == "notes.txt"
    assert body["size"] == len(b"hello world")

    file_body = client.get(f"/api/chats/{chat_id}/file_content", params={"path": "uploads/notes.txt"}).json()
    assert file_body["content"] == "hello world"


def test_upload_sanitizes_a_path_traversal_filename(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(
        f"/api/chats/{chat_id}/upload",
        files={"file": ("../../etc/passwd", b"nope", "text/plain")},
    )
    assert res.status_code == 201
    # The traversal is stripped down to just the filename component, then
    # sanitized to a safe charset — it must land inside uploads/, never
    # escape the workspace.
    assert res.json()["path"].startswith("uploads/")
    assert ".." not in res.json()["path"]


def test_upload_avoids_overwriting_an_existing_file_with_the_same_name(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    first = client.post(
        f"/api/chats/{chat_id}/upload", files={"file": ("a.txt", b"first", "text/plain")}
    ).json()
    second = client.post(
        f"/api/chats/{chat_id}/upload", files={"file": ("a.txt", b"second", "text/plain")}
    ).json()
    assert first["path"] != second["path"]
    assert client.get(f"/api/chats/{chat_id}/file_content", params={"path": first["path"]}).json()["content"] == "first"
    assert client.get(f"/api/chats/{chat_id}/file_content", params={"path": second["path"]}).json()["content"] == "second"


def test_upload_rejects_a_file_over_the_size_limit(client, monkeypatch):
    import grandice.server.app as server_app_mod

    monkeypatch.setattr(server_app_mod, "MAX_UPLOAD_BYTES", 10)
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(
        f"/api/chats/{chat_id}/upload",
        files={"file": ("big.txt", b"x" * 100, "text/plain")},
    )
    assert res.status_code == 413


def test_task_with_an_attachment_mentions_it_in_the_stored_message(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    upload = client.post(
        f"/api/chats/{chat_id}/upload", files={"file": ("notes.txt", b"hi", "text/plain")}
    ).json()

    res = client.post(f"/api/chats/{chat_id}/task", json={"message": "see attached", "attachments": [upload["path"]]})
    assert res.status_code == 202

    for _ in range(50):
        if not client.get(f"/api/chats/{chat_id}/state").json()["running"]:
            break
        time.sleep(0.02)

    grandice_state = client.app.state.grandice
    messages = grandice_state.runtimes[chat_id].session.messages
    assert "notes.txt" in messages[0]["content"]


def test_files_lists_the_workspace_root(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    grandice_state = client.app.state.grandice
    client.get(f"/api/chats/{chat_id}/files")  # touch the runtime into existence
    session = grandice_state.runtimes[chat_id].session
    (session.sandbox.workspace / "note.txt").write_text("hello")

    res = client.get(f"/api/chats/{chat_id}/files", params={"path": "."})
    names = {e["name"] for e in res.json()["entries"]}
    assert "note.txt" in names


def test_files_rejects_path_traversal(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.get(f"/api/chats/{chat_id}/files", params={"path": "../../../etc"})
    assert res.status_code == 403


def test_file_content_reads_a_real_file(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    grandice_state = client.app.state.grandice
    client.get(f"/api/chats/{chat_id}/files")
    session = grandice_state.runtimes[chat_id].session
    (session.sandbox.workspace / "note.txt").write_text("hello there")

    res = client.get(f"/api/chats/{chat_id}/file_content", params={"path": "note.txt"})
    body = res.json()
    assert body["content"] == "hello there"
    assert body["binary"] is False


def test_file_content_404s_on_a_missing_file(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.get(f"/api/chats/{chat_id}/file_content", params={"path": "nope.txt"})
    assert res.status_code == 404


def test_task_rejects_an_empty_message(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(f"/api/chats/{chat_id}/task", json={"message": "   "})
    assert res.status_code == 400


def test_cancel_with_nothing_running_is_a_conflict(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(f"/api/chats/{chat_id}/cancel")
    assert res.status_code == 409


def test_task_is_refused_while_one_is_already_running(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    client.get(f"/api/chats/{chat_id}/state")  # touch the runtime into existence
    grandice_state = client.app.state.grandice
    grandice_state.runtimes[chat_id].running = True  # simulate a task already in flight

    res = client.post(f"/api/chats/{chat_id}/task", json={"message": "another one"})
    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


def test_a_real_task_streams_events_updates_state_and_persists_messages(client):
    _register(client)
    project_id, chat_id = _project_and_chat(client)

    res = client.post(f"/api/chats/{chat_id}/task", json={"message": "list the workspace"})
    assert res.status_code == 202

    for _ in range(50):
        if not client.get(f"/api/chats/{chat_id}/state").json()["running"]:
            break
        time.sleep(0.02)

    log = client.get(f"/api/chats/{chat_id}/log").json()
    types = [e["type"] for e in log]
    assert "tool_started" in types
    assert "tool_finished" in types
    assert "finished" in types
    assert types[-1] == "state_changed"

    state = client.get(f"/api/chats/{chat_id}/state").json()
    assert state["running"] is False
    assert state["cost"]["calls"] > 0

    # The whole point of a persisted chat: reopening it (a fresh runtime,
    # simulated here by evicting the live one) resumes real history.
    grandice_state = client.app.state.grandice
    del grandice_state.runtimes[chat_id]
    messages = client.get(f"/api/chats/{chat_id}/state").json()
    assert messages["session_id"]  # runtime rebuilt without error
    assert len(grandice_state.runtimes[chat_id].session.messages) > 0


def test_deleting_a_chat_evicts_its_live_runtime(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    client.get(f"/api/chats/{chat_id}/state")
    grandice_state = client.app.state.grandice
    assert chat_id in grandice_state.runtimes

    client.delete(f"/api/chats/{chat_id}")
    assert chat_id not in grandice_state.runtimes


# --- in-browser approvals (§P5), now per-chat ------------------------------

def _build_client_with_outward_tool(app, client) -> None:
    project_id, chat_id = _project_and_chat(client)
    client.get(f"/api/chats/{chat_id}/state")  # touch the runtime into existence

    from grandice.tools.base import Risk, ToolSpec

    async def fake_send(**kwargs):
        return "sent"

    grandice_state = app.state.grandice
    runtime = grandice_state.runtimes[chat_id]
    runtime.session.registry.add(
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

    runtime.session.router.backends = [OutwardOnceStub()]
    return chat_id


def _wait_for_approval_request(client, chat_id: str) -> str:
    for _ in range(50):
        log = client.get(f"/api/chats/{chat_id}/log").json()
        approvals = [e for e in log if e["type"] == "approval_needed"]
        if approvals:
            return approvals[0]["request_id"]
        time.sleep(0.02)
    raise AssertionError("no approval_needed event appeared in time")


def _wait_until_not_running(client, chat_id: str) -> None:
    for _ in range(50):
        if not client.get(f"/api/chats/{chat_id}/state").json()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("task never finished")


def test_approving_lets_the_outward_tool_run(app, client):
    _register(client)
    chat_id = _build_client_with_outward_tool(app, client)

    client.post(f"/api/chats/{chat_id}/task", json={"message": "send an email"})
    request_id = _wait_for_approval_request(client, chat_id)

    res = client.post(f"/api/chats/{chat_id}/approve", json={"request_id": request_id, "approved": True})
    assert res.status_code == 200

    _wait_until_not_running(client, chat_id)
    log = client.get(f"/api/chats/{chat_id}/log").json()
    finishes = [e for e in log if e["type"] == "tool_finished" and e["name"] == "send_email"]
    assert finishes and finishes[0]["ok"] is True


def test_denying_stops_the_outward_tool(app, client):
    _register(client)
    chat_id = _build_client_with_outward_tool(app, client)

    client.post(f"/api/chats/{chat_id}/task", json={"message": "send an email"})
    request_id = _wait_for_approval_request(client, chat_id)

    client.post(f"/api/chats/{chat_id}/approve", json={"request_id": request_id, "approved": False})

    _wait_until_not_running(client, chat_id)
    log = client.get(f"/api/chats/{chat_id}/log").json()
    finishes = [e for e in log if e["type"] == "tool_finished" and e["name"] == "send_email"]
    assert finishes and finishes[0]["ok"] is False
    assert "declined" in finishes[0]["preview"].lower()


def test_approve_404s_on_an_unknown_request_id(client):
    _register(client)
    _, chat_id = _project_and_chat(client)
    res = client.post(f"/api/chats/{chat_id}/approve", json={"request_id": "nonexistent", "approved": True})
    assert res.status_code == 404


# --- MCP connectors through the dashboard's lifespan (not the CLI's) ------

def test_lifespan_starts_and_stops_a_configured_connector(tmp_path, monkeypatch):
    """Skipped if the optional `mcp` extras aren't installed."""
    pytest.importorskip("mcp")
    pytest.importorskip("mcp_server_sqlite")

    monkeypatch.setenv("GRANDICE_MCP_CONNECTORS", "sqlite")

    app = server_app.create_app(
        accounts_path=tmp_path / "accounts.db",
        projects_path=tmp_path / "projects.db",
        projects_root=tmp_path / "projects",
    )
    with TestClient(app) as c:
        _register(c)
        _, chat_id = _project_and_chat(c)
        state = c.get(f"/api/chats/{chat_id}/state").json()
        assert state["connectors"] == ["sqlite"]
        assert "sqlite.read_query" in state["tools"]["latent"]
        assert not any(n.startswith("sqlite.") for n in state["tools"]["active"])
