"""One connected smoke test across every phase, P0 through P4 — not a
replacement for the 100 per-component tests, a check that the pieces they
verify in isolation actually work together in one real session.

Uses the stub backend (zero cost, zero network to a model) but every other
piece is real: real sandbox, real files on disk, a real MCP subprocess, a
real SQLite task queue, a real subagent loop. Skips the P3 portion cleanly
if the optional `mcp` extras aren't installed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from grandice import loop as agent_loop
from grandice.config import Config
from grandice.permissions import Gate, always_deny
from grandice.router import ToolCall
from grandice.session import build as build_session, start_connectors, stop_connectors

pytest.importorskip("mcp")
pytest.importorskip("mcp_server_sqlite")


@pytest.fixture
async def session(tmp_path: Path):
    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        api_key=None,
        base_url=None,
        mcp_connectors=("sqlite",),
    )
    s = build_session(config, Gate(always_deny))
    await start_connectors(s)
    yield s
    await stop_connectors(s)
    s.tasks.close()


async def test_every_phase_p0_through_p4_in_one_session(session):
    # --- P0: loop + core tools -------------------------------------------
    r = await agent_loop.execute(session, ToolCall("1", "write", {"path": "notes.txt", "content": "hello"}))
    assert not r["content"].startswith("ERROR:")

    r = await agent_loop.execute(session, ToolCall("1", "read", {"path": "notes.txt"}))
    assert "hello" in r["content"]

    r = await agent_loop.execute(session, ToolCall("1", "glob", {"pattern": "*.txt"}))
    assert "notes.txt" in r["content"]

    r = await agent_loop.execute(
        session, ToolCall("1", "todo", {"items": [{"task": "verify everything", "status": "in_progress"}]})
    )
    assert not r["content"].startswith("ERROR:")
    assert session.todos.items  # the loop's plan-tracking actually persisted

    r = await agent_loop.execute(session, ToolCall("1", "bash", {"command": "echo real shell"}))
    assert "real shell" in r["content"]

    # A full run_turn pass too, not just individual tool dispatch — the stub
    # backend's scripted first turn calls glob, second turn gives final text.
    events = [e async for e in agent_loop.run_turn(session, "list the workspace")]
    assert any(isinstance(e, agent_loop.ToolStarted) for e in events)
    assert any(isinstance(e, agent_loop.Finished) for e in events)

    # --- P1: sandbox choice, audit log, cost ledger, rate limiting --------
    assert session.sandbox.name in ("sandbox-exec", "docker", "none")
    assert session.log_path.exists() and session.log_path.stat().st_size > 0
    assert session.router.ledger.calls > 0
    assert session.router.rate_limiter is None  # stub mode: nothing to pace

    # --- P2: skills discovered and loadable --------------------------------
    assert {s.name for s in session.skills} == {"xlsx", "pptx"}
    r = await agent_loop.execute(session, ToolCall("1", "load_skill", {"name": "xlsx"}))
    assert "openpyxl" in r["content"]

    # --- P3: MCP connector latent, search_tools activates it, real call ---
    assert any(n.startswith("sqlite.") for n in [s.name for s in session.registry.latent()])
    r = await agent_loop.execute(session, ToolCall("1", "search_tools", {"query": "create_table"}))
    assert "sqlite.create_table" in session.registry.names()  # now active

    r = await agent_loop.execute(
        session, ToolCall("1", "sqlite.create_table", {"query": "CREATE TABLE t (id INTEGER)"})
    )
    assert not r["content"].startswith("ERROR:")
    assert (session.sandbox.workspace / "workspace.db").exists()  # a real file, not a mock

    # --- P4: subagent isolation + background task queue --------------------
    r = await agent_loop.execute(session, ToolCall("1", "spawn_subagent", {"task": "list the workspace"}))
    assert "[subagent:" in r["content"]

    r = await agent_loop.execute(session, ToolCall("1", "spawn_background", {"task": "list the workspace"}))
    task_id = r["content"].split("'")[1]
    import asyncio
    for _ in range(50):
        if session.tasks.get(task_id).status.value == "done":
            break
        await asyncio.sleep(0.02)
    assert session.tasks.get(task_id).status.value == "done"

    r = await agent_loop.execute(session, ToolCall("1", "list_tasks", {}))
    assert task_id in r["content"]
