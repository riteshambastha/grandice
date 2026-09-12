"""FastAPI + SSE live-view dashboard (§09's stack pick — "streaming without
WebSocket complexity"). One process, one Session, any number of browser tabs
watching it: observe the loop live, send a task, cancel one. Not the full P5
client — see server/__init__.py for what's deliberately not here yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import loop as agent_loop
from ..config import Config
from ..permissions import Gate, always_deny
from ..session import Session, build as build_session, start_connectors, stop_connectors
from .broadcast import Broadcaster
from .serialize import event_to_dict

STATIC_DIR = Path(__file__).parent / "static"
KEEPALIVE_SECONDS = 15
FILE_PREVIEW_LIMIT = 100_000  # characters — same truncate-on-the-way-in spirit as §05.4


class TaskIn(BaseModel):
    message: str


class AppState:
    """Shared mutable state, held on the FastAPI app rather than as module
    globals — so create_app() can build more than one, in tests."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.broadcaster = Broadcaster()
        self.running = False


def create_app(session: Session | None = None) -> FastAPI:
    state = AppState(session or build_session(Config.from_env(), Gate(always_deny)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Starting connectors needs a running event loop — build_session()
        # above is sync and does not do this itself (see session.py).
        await start_connectors(state.session)
        try:
            yield
        finally:
            await stop_connectors(state.session)

    app = FastAPI(title="grandice", lifespan=lifespan)
    app.state.grandice = state  # exposed for tests; routes below close over `state` directly

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return _snapshot(state)

    @app.get("/api/log")
    async def get_log() -> list[dict[str, Any]]:
        return state.broadcaster.history

    @app.get("/api/files")
    async def list_files(path: str = ".") -> dict[str, Any]:
        return _list_dir(state.session, path)

    @app.get("/api/file_content")
    async def file_content(path: str) -> dict[str, Any]:
        return _read_file(state.session, path)

    @app.post("/api/task", status_code=202)
    async def start_task(body: TaskIn) -> dict[str, Any]:
        if not body.message.strip():
            raise HTTPException(400, "message must not be empty.")
        if state.running:
            raise HTTPException(409, "A task is already running — cancel it first, or wait.")
        # No `await` between the check above and this set, so two overlapping
        # requests on the same event loop cannot both pass the check.
        state.running = True
        state.session.cancelled = False
        asyncio.create_task(_run(state, body.message))
        return {"status": "started"}

    @app.post("/api/cancel")
    async def cancel_task() -> dict[str, Any]:
        if not state.running:
            raise HTTPException(409, "No task is running.")
        state.session.cancelled = True
        return {"status": "cancelling"}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(_stream(state, request), media_type="text/event-stream")

    # Mounted last: StaticFiles(html=True) serves index.html at "/" and would
    # otherwise shadow any /api/* route registered after it.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def _run(state: AppState, message: str) -> None:
    try:
        async for event in agent_loop.run_turn(state.session, message):
            state.broadcaster.publish(event_to_dict(event))
    finally:
        state.running = False
        # Plan/cost/files may all have changed; tell the UI to refetch rather
        # than trying to diff every field into an event of its own.
        state.broadcaster.publish({"type": "state_changed"})


async def _stream(state: AppState, request: Request):
    queue = state.broadcaster.subscribe()
    try:
        for event in state.broadcaster.history:
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
        state.broadcaster.unsubscribe(queue)


def _snapshot(state: AppState) -> dict[str, Any]:
    session = state.session
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
        "running": state.running,
        "plan": session.todos.items,
        "skills": [{"name": s.name, "description": s.description} for s in session.skills],
        "connectors": [c.spec.name for c in session.connectors],
        "tools": {
            "active": session.registry.names(),
            "latent": [s.name for s in session.registry.latent()],
        },
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
             "EC2 instance unless you deliberately change this. Use an SSH tunnel "
             "(`ssh -L 8000:localhost:8000 ...`) to view a remote instance instead.",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
