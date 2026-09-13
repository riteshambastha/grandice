"""FastAPI + SSE live-view dashboard (§09's stack pick — "streaming without
WebSocket complexity"). Multi-user, multi-project, multi-chat: real
accounts (accounts.py), projects each with their own workspace/sandbox
(projects.py), any number of chats per project — each chat gets its own
isolated ChatRuntime (Session, event broadcaster, running flag, pending
approvals), so two chats never see each other's stream or block each
other's task.

Not the full P5 client to the letter — see server/__init__.py — and this
is dashboard/desktop-app only; the plain `grandice` CLI stays the
single-user, no-login tool it always was.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import loop as agent_loop
from ..accounts import AccountStore, InvalidCredentials, UsernameTaken, User
from ..config import Config
from ..permissions import Gate
from ..projects import Chat, NotFound, ProjectStore
from ..session import Session, build as build_session, start_connectors, stop_connectors
from .broadcast import Broadcaster
from .serialize import event_to_dict

STATIC_DIR = Path(__file__).parent / "static"
KEEPALIVE_SECONDS = 15
FILE_PREVIEW_LIMIT = 100_000  # characters — same truncate-on-the-way-in spirit as §05.4
APPROVAL_TIMEOUT_SECONDS = 300  # an unanswered approval denies itself rather than hanging the turn forever
TASK_POLL_SECONDS = 2  # how often the background-task poller checks for status changes

SESSION_COOKIE = "grandice_session"
ACCOUNTS_DB_PATH = Path.home() / ".grandice" / "accounts.db"
PROJECTS_DB_PATH = Path.home() / ".grandice" / "projects.db"
PROJECTS_ROOT = Path.home() / "Documents" / "grandice" / "projects"


class RegisterIn(BaseModel):
    username: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class ProjectIn(BaseModel):
    name: str


class ChatIn(BaseModel):
    title: str = "New chat"


class TaskIn(BaseModel):
    message: str


class ApproveIn(BaseModel):
    request_id: str
    approved: bool


@dataclass
class ChatRuntime:
    """Everything one open chat needs that used to be the whole AppState —
    its own Session, its own event stream, its own running/approval state.
    Built lazily the first time a chat is actually touched, not at login."""

    session: Session
    broadcaster: Broadcaster = field(default_factory=Broadcaster)
    running: bool = False
    pending_approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    # The mcp SDK's AsyncExitStack is task-bound (anyio cancel scopes must
    # exit in the same task that entered them) — since a chat's runtime is
    # built inside whichever request task first touches it, connectors must
    # be opened *and closed* from one dedicated task that outlives that
    # request, not from the request task or the lifespan's own task. `_owner`
    # is that task; `_closing` is how _evict_runtime asks it to shut down.
    _owner: asyncio.Task | None = field(default=None, repr=False)
    _closing: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class AppState:
    def __init__(self, base_config: Config, accounts: AccountStore, projects: ProjectStore, projects_root: Path) -> None:
        self.base_config = base_config
        self.accounts = accounts
        self.projects = projects
        self.projects_root = projects_root
        self.runtimes: dict[str, ChatRuntime] = {}


def create_app(
    base_config: Config | None = None,
    accounts_path: Path | None = None,
    projects_path: Path | None = None,
    projects_root: Path | None = None,
) -> FastAPI:
    state = AppState(
        base_config=base_config or Config.from_env(),
        accounts=AccountStore(accounts_path or ACCOUNTS_DB_PATH),
        projects=ProjectStore(projects_path or PROJECTS_DB_PATH),
        projects_root=projects_root or PROJECTS_ROOT,
    )

    def current_user(request: Request) -> User:
        token = request.cookies.get(SESSION_COOKIE)
        user = state.accounts.resolve_session(token) if token else None
        if user is None:
            raise HTTPException(401, "Not logged in, or your session expired — log in again.")
        return user

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        poller = asyncio.create_task(_poll_tasks(state))
        try:
            yield
        finally:
            poller.cancel()
            for chat_id in list(state.runtimes):
                await _evict_runtime(state, chat_id)
            state.accounts.close()
            state.projects.close()

    app = FastAPI(title="grandice", lifespan=lifespan)
    app.state.grandice = state  # exposed for tests; routes below close over `state` directly

    # --- auth ---------------------------------------------------------

    @app.post("/api/auth/register", status_code=201)
    async def register(body: RegisterIn, response: Response) -> dict[str, Any]:
        try:
            user = state.accounts.register(body.username, body.password)
        except (ValueError, UsernameTaken) as exc:
            raise HTTPException(400, str(exc)) from exc
        _set_session_cookie(response, state.accounts.create_session(user.id))
        return {"id": user.id, "username": user.username}

    @app.post("/api/auth/login")
    async def login(body: LoginIn, response: Response) -> dict[str, Any]:
        try:
            user = state.accounts.authenticate(body.username, body.password)
        except InvalidCredentials as exc:
            raise HTTPException(401, str(exc)) from exc
        _set_session_cookie(response, state.accounts.create_session(user.id))
        return {"id": user.id, "username": user.username}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            state.accounts.delete_session(token)
        response.delete_cookie(SESSION_COOKIE)
        return {"status": "ok"}

    @app.get("/api/auth/me")
    async def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return {"id": user.id, "username": user.username}

    # --- projects -------------------------------------------------------

    @app.get("/api/projects")
    async def list_projects(user: User = Depends(current_user)) -> list[dict[str, Any]]:
        return [_project_dict(p) for p in state.projects.list_projects(user.id)]

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectIn, user: User = Depends(current_user)) -> dict[str, Any]:
        return _project_dict(state.projects.create_project(user.id, body.name))

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        try:
            chats = state.projects.list_chats(project_id, user.id)
            state.projects.delete_project(project_id, user.id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        for chat in chats:
            await _evict_runtime(state, chat.id)
        return {"status": "deleted"}

    # --- chats ------------------------------------------------------------

    @app.get("/api/projects/{project_id}/chats")
    async def list_chats(project_id: str, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        try:
            return [_chat_dict(c) for c in state.projects.list_chats(project_id, user.id)]
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/projects/{project_id}/chats", status_code=201)
    async def create_chat(project_id: str, body: ChatIn, user: User = Depends(current_user)) -> dict[str, Any]:
        try:
            return _chat_dict(state.projects.create_chat(project_id, user.id, body.title))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(chat_id: str, body: ChatIn, user: User = Depends(current_user)) -> dict[str, Any]:
        try:
            state.projects.rename_chat(chat_id, user.id, body.title)
            return _chat_dict(state.projects.get_chat(chat_id, user.id))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        try:
            state.projects.delete_chat(chat_id, user.id)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        await _evict_runtime(state, chat_id)
        return {"status": "deleted"}

    # --- one chat's runtime: state, files, task, cancel, approve, events ---

    @app.get("/api/chats/{chat_id}/state")
    async def chat_state(chat_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        _, runtime = await _resolve(state, chat_id, user)
        return _snapshot(runtime)

    @app.get("/api/chats/{chat_id}/log")
    async def chat_log(chat_id: str, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        _, runtime = await _resolve(state, chat_id, user)
        return runtime.broadcaster.history

    @app.get("/api/chats/{chat_id}/files")
    async def chat_files(chat_id: str, path: str = ".", user: User = Depends(current_user)) -> dict[str, Any]:
        _, runtime = await _resolve(state, chat_id, user)
        return _list_dir(runtime.session, path)

    @app.get("/api/chats/{chat_id}/file_content")
    async def chat_file_content(chat_id: str, path: str, user: User = Depends(current_user)) -> dict[str, Any]:
        _, runtime = await _resolve(state, chat_id, user)
        return _read_file(runtime.session, path)

    @app.post("/api/chats/{chat_id}/task", status_code=202)
    async def chat_task(chat_id: str, body: TaskIn, user: User = Depends(current_user)) -> dict[str, Any]:
        if not body.message.strip():
            raise HTTPException(400, "message must not be empty.")
        chat, runtime = await _resolve(state, chat_id, user)
        if runtime.running:
            raise HTTPException(409, "A task is already running in this chat — cancel it first, or wait.")
        # No `await` between the check above and this set, so two overlapping
        # requests on the same event loop cannot both pass the check.
        runtime.running = True
        runtime.session.cancelled = False
        asyncio.create_task(_run(state, chat, runtime, body.message))
        return {"status": "started"}

    @app.post("/api/chats/{chat_id}/cancel")
    async def chat_cancel(chat_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        _, runtime = await _resolve(state, chat_id, user)
        if not runtime.running:
            raise HTTPException(409, "No task is running.")
        runtime.session.cancelled = True
        return {"status": "cancelling"}

    @app.post("/api/chats/{chat_id}/approve")
    async def chat_approve(chat_id: str, body: ApproveIn, user: User = Depends(current_user)) -> dict[str, Any]:
        _, runtime = await _resolve(state, chat_id, user)
        future = runtime.pending_approvals.get(body.request_id)
        if future is None or future.done():
            raise HTTPException(404, "No pending approval with that id — it may have already timed out or been answered.")
        future.set_result(body.approved)
        return {"status": "ok"}

    @app.get("/api/chats/{chat_id}/events")
    async def chat_events(chat_id: str, request: Request, user: User = Depends(current_user)) -> StreamingResponse:
        _, runtime = await _resolve(state, chat_id, user)
        return StreamingResponse(_stream(runtime, request), media_type="text/event-stream")

    # Mounted last: StaticFiles(html=True) serves index.html at "/" and would
    # otherwise shadow any /api/* route registered after it.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _set_session_cookie(response: Response, token: str) -> None:
    from .. import accounts as accounts_mod

    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=accounts_mod.SESSION_LIFETIME_SECONDS, httponly=True, samesite="lax",
    )


def _project_dict(p) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "created_at": p.created_at}


def _chat_dict(c: Chat) -> dict[str, Any]:
    return {"id": c.id, "project_id": c.project_id, "title": c.title,
            "created_at": c.created_at, "updated_at": c.updated_at}


async def _resolve(state: AppState, chat_id: str, user: User) -> tuple[Chat, ChatRuntime]:
    """Look up a chat (raising 404 if it doesn't exist or isn't this user's —
    the same NotFound either way, so a request can't tell those apart) and
    its runtime, building the runtime on first touch."""
    try:
        chat = state.projects.get_chat(chat_id, user.id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if chat_id not in state.runtimes:
        state.runtimes[chat_id] = await _build_runtime(state, chat)
    return chat, state.runtimes[chat_id]


async def _build_runtime(state: AppState, chat: Chat) -> ChatRuntime:
    broadcaster = Broadcaster()
    pending_approvals: dict[str, asyncio.Future] = {}

    async def web_ask(payload: str) -> bool:
        """This chat's Asker (§08/permissions.py): publish an approval_needed
        event on *this chat's* broadcaster and await a Future that
        /api/chats/{id}/approve resolves — the CLI's synchronous terminal
        prompt, adapted to a per-chat event stream instead of one global one."""
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        pending_approvals[request_id] = future
        broadcaster.publish({"type": "approval_needed", "request_id": request_id, "payload": payload})
        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_approvals.pop(request_id, None)

    project_config = replace(state.base_config, workspace=_project_workspace(state, chat.project_id))
    session = build_session(project_config, Gate(web_ask))
    session.messages = state.projects.load_messages(chat.id, chat.user_id)

    runtime = ChatRuntime(session=session, broadcaster=broadcaster, pending_approvals=pending_approvals)
    if session.connectors:
        ready = asyncio.Event()
        runtime._owner = asyncio.create_task(_own_connectors(runtime, ready))
        await ready.wait()
    return runtime


async def _own_connectors(runtime: ChatRuntime, ready: asyncio.Event) -> None:
    """Lives for exactly as long as this chat's connectors need to stay
    open — started and stopped from this one task, never the request task
    that triggered the build or the lifespan's own shutdown task. See
    ChatRuntime's own comment for why that distinction matters here."""
    await start_connectors(runtime.session)
    ready.set()
    await runtime._closing.wait()
    await stop_connectors(runtime.session)


async def _evict_runtime(state: AppState, chat_id: str) -> None:
    runtime = state.runtimes.pop(chat_id, None)
    if runtime is not None:
        runtime._closing.set()
        if runtime._owner is not None:
            await runtime._owner
        runtime.session.tasks.close()


def _project_workspace(state: AppState, project_id: str) -> Path:
    return state.projects_root / project_id / "workspace"


async def _run(state: AppState, chat: Chat, runtime: ChatRuntime, message: str) -> None:
    try:
        async for event in agent_loop.run_turn(runtime.session, message):
            runtime.broadcaster.publish(event_to_dict(event))
    finally:
        runtime.running = False
        # A chat's whole point is resuming exactly where it left off — save
        # after every turn, not just on a clean shutdown.
        state.projects.save_messages(chat.id, chat.user_id, runtime.session.messages)
        # Plan/cost/files may all have changed; tell the UI to refetch rather
        # than trying to diff every field into an event of its own.
        runtime.broadcaster.publish({"type": "state_changed"})


async def _poll_tasks(state: AppState) -> None:
    """Background tasks (spawn_background, §P4) run outside the SSE
    broadcaster entirely — scheduled from inside a tool call, not from _run
    above. Without this, a task's completion would only show up on
    whatever unrelated state refresh happened to come next. One poller
    covers every live chat's runtime, not just one global session."""
    last: dict[str, dict[str, str]] = {}
    try:
        while True:
            await asyncio.sleep(TASK_POLL_SECONDS)
            for chat_id, runtime in list(state.runtimes.items()):
                current = {t.id: t.status.value for t in runtime.session.tasks.list()}
                if current != last.get(chat_id):
                    runtime.broadcaster.publish({"type": "state_changed"})
                    last[chat_id] = current
    except asyncio.CancelledError:
        pass


async def _stream(runtime: ChatRuntime, request: Request):
    queue = runtime.broadcaster.subscribe()
    try:
        for event in runtime.broadcaster.history:
            yield f"data: {json.dumps(event)}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"  # comment line — keeps proxies from closing the connection
    finally:
        runtime.broadcaster.unsubscribe(queue)


def _snapshot(runtime: ChatRuntime) -> dict[str, Any]:
    session = runtime.session
    ledger = session.router.ledger
    limiter = session.router.rate_limiter
    return {
        "session_id": session.id,
        "live": session.config.live,
        "model": {
            "orchestrator": session.config.tiers.orchestrator,
            "worker": session.config.tiers.worker,
            "bulk": session.config.tiers.bulk,
        },
        "sandbox": session.sandbox.name,
        "running": runtime.running,
        "plan": session.todos.items,
        "skills": [{"name": s.name, "description": s.description} for s in session.skills],
        "connectors": [c.spec.name for c in session.connectors],
        "tools": {
            "active": session.registry.names(),
            "latent": [s.name for s in session.registry.latent()],
        },
        "tasks": [
            {
                "id": t.id, "status": t.status.value, "description": t.description,
                "result": t.result, "error": t.error,
            }
            for t in session.tasks.list()[:20]  # newest first, already ordered by the store
        ],
        "cost": {
            "spent_usd": round(ledger.spent_usd, 4),
            "cap_usd": ledger.cap_usd,
            "calls": ledger.calls,
            "prompt_tokens": ledger.prompt_tokens,
            "completion_tokens": ledger.completion_tokens,
        },
        "rate_limit": (
            {
                "used_today": limiter.used_today,
                "per_day": limiter.per_day,
                "per_minute": limiter.per_minute,
            }
            if limiter is not None
            else None  # stub mode: nothing to pace
        ),
    }


def _list_dir(session: Session, rel_path: str) -> dict[str, Any]:
    try:
        target = session.sandbox.resolve(rel_path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not target.exists():
        raise HTTPException(404, f"{rel_path} does not exist.")
    if not target.is_dir():
        raise HTTPException(400, f"{rel_path} is not a directory.")

    entries = [
        {
            "name": child.name,
            "path": str(child.relative_to(session.sandbox.workspace)),
            "is_dir": child.is_dir(),
            "size": None if child.is_dir() else child.stat().st_size,
        }
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    ]
    return {"path": rel_path, "entries": entries}


def _read_file(session: Session, rel_path: str) -> dict[str, Any]:
    try:
        target = session.sandbox.resolve(rel_path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not target.exists():
        raise HTTPException(404, f"{rel_path} does not exist.")
    if target.is_dir():
        raise HTTPException(400, f"{rel_path} is a directory.")

    try:
        text = target.read_text()
    except UnicodeDecodeError:
        return {"path": rel_path, "binary": True, "content": "", "truncated": False}

    return {
        "path": rel_path,
        "binary": False,
        "content": text[:FILE_PREVIEW_LIMIT],
        "truncated": len(text) > FILE_PREVIEW_LIMIT,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the grandice live-view dashboard.")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address. Default is localhost-only — nothing is exposed even on an "
             "EC2 instance unless you deliberately change this. Real accounts now guard "
             "the data, but see accounts.py's own note: that's not the same as transport "
             "security. Use HTTPS (a reverse proxy) or a VPN before binding this to "
             "anything a shared network can reach.",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
