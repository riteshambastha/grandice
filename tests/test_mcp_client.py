"""MCP connector tests, run against the real community mcp-server-sqlite
subprocess — not a mock. Skipped entirely when the optional `mcp` extras
aren't installed, since mcp_client.py is only ever imported when a connector
is actually configured.

Connector start/stop is deliberately NOT an async pytest fixture here: an
anyio task group opens inside `start()`, and an async generator fixture can
run its setup and the test body in different task contexts under
pytest-asyncio, which trips anyio's "exit cancel scope in a different task"
guard on teardown — a real interaction quirk, not a bug in mcp_client.py
(a plain `asyncio.run()` script never hits it, only pytest's fixture
machinery does). Keeping the whole lifecycle inside one test coroutine avoids
the boundary entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
pytest.importorskip("mcp_server_sqlite")

from grandice import mcp_client  # noqa: E402
from grandice.tools.base import Registry, Risk  # noqa: E402


async def test_spec_for_rejects_unknown_connector_names(tmp_path):
    with pytest.raises(ValueError, match="Unknown connector"):
        mcp_client.spec_for("not-a-real-connector", tmp_path, "x.db")


async def test_discovers_the_real_sqlite_tool_set_with_verified_risk(tmp_path):
    spec = mcp_client.spec_for("sqlite", tmp_path, "test.db")
    conn = mcp_client.Connector(spec)
    await conn.start()
    try:
        specs = await conn.tool_specs()
        by_name = {s.name.removeprefix("sqlite."): s for s in specs}

        assert {"read_query", "write_query", "create_table", "list_tables", "describe_table"} <= set(by_name)
        assert by_name["read_query"].risk is Risk.READ
        assert by_name["write_query"].risk is Risk.WRITE
        assert all(s.name.startswith("sqlite.") for s in specs)  # namespaced against collisions
    finally:
        await conn.stop()


async def test_a_real_query_round_trips_through_the_wrapped_tool(tmp_path):
    spec = mcp_client.spec_for("sqlite", tmp_path, "test.db")
    conn = mcp_client.Connector(spec)
    await conn.start()
    try:
        specs = {s.name: s for s in await conn.tool_specs()}
        await specs["sqlite.create_table"].run(query="CREATE TABLE t (id INTEGER, name TEXT)")
        await specs["sqlite.write_query"].run(query="INSERT INTO t VALUES (1, 'widget')")
        result = await specs["sqlite.read_query"].run(query="SELECT * FROM t")
        assert "widget" in result
    finally:
        await conn.stop()


async def test_a_failed_query_raises_despite_the_servers_iserror_false(tmp_path):
    """mcp-server-sqlite reports failures as isError=False with the error
    embedded in the text (confirmed live, 2026-09-12) — this is the fallback
    that catches it anyway, verified against the server's real wording."""
    spec = mcp_client.spec_for("sqlite", tmp_path, "test.db")
    conn = mcp_client.Connector(spec)
    await conn.start()
    try:
        specs = {s.name: s for s in await conn.tool_specs()}
        with pytest.raises(mcp_client.ConnectorError, match="no such table"):
            await specs["sqlite.read_query"].run(query="SELECT * FROM nonexistent_table")
    finally:
        await conn.stop()


async def test_start_connectors_registers_tools_as_latent(tmp_path):
    spec = mcp_client.spec_for("sqlite", tmp_path, "test.db")
    connector = mcp_client.Connector(spec)
    registry = Registry()

    await mcp_client.start_connectors([connector], registry)
    try:
        assert registry.names() == []  # nothing active yet
        assert {s.name for s in registry.latent()} >= {
            f"sqlite.{n}" for n in
            ("read_query", "write_query", "create_table", "list_tables", "describe_table")
        }
    finally:
        await mcp_client.stop_connectors([connector])


# --- full stack: config -> session -> connector -> search_tools -> call ---

async def test_full_stack_config_to_real_sqlite_call(tmp_path):
    """Everything P3 adds, wired together exactly as a real run would use
    it: a configured connector becomes a latent tool at session build, gets
    activated by search_tools, and is then callable — a real sqlite write
    landing on disk, not a mock standing in for any of these pieces."""
    from dataclasses import replace

    from grandice import loop as agent_loop
    from grandice.config import Config
    from grandice.permissions import Gate, always_deny
    from grandice.router import ToolCall
    from grandice.session import build as build_session, start_connectors, stop_connectors

    config = replace(
        Config.from_env(),
        workspace=tmp_path / "ws",
        sandbox="sandbox-exec",
        api_key=None,
        base_url=None,
        mcp_connectors=("sqlite",),
    )
    session = build_session(config, Gate(always_deny))
    assert session.connectors and session.connectors[0].spec.name == "sqlite"

    await start_connectors(session)
    try:
        # Not active yet — this is the whole point of latent registration.
        assert not any(n.startswith("sqlite.") for n in session.registry.names())

        # "sqlite write" (with a space) would never substring-match anything
        # and silently fall into the no-match branch — query on a real
        # substring of the tool name instead.
        search_result = await agent_loop.execute(
            session, ToolCall("1", "search_tools", {"query": "write_query"})
        )
        assert "Activated" in search_result["content"]
        assert "sqlite.write_query" in search_result["content"]
        assert "sqlite.write_query" in session.registry.names()  # now active

        create = await agent_loop.execute(
            session, ToolCall("1", "sqlite.create_table",
                               {"query": "CREATE TABLE t (id INTEGER, name TEXT)"}),
        )
        assert not create["content"].startswith("ERROR:")

        write = await agent_loop.execute(
            session, ToolCall("1", "sqlite.write_query", {"query": "INSERT INTO t VALUES (1, 'widget')"}),
        )
        assert not write["content"].startswith("ERROR:")

        read = await agent_loop.execute(
            session, ToolCall("1", "sqlite.read_query", {"query": "SELECT * FROM t"}),
        )
        assert "widget" in read["content"]

        # Real file on disk, at the configured workspace-relative path — not
        # just a well-formed response.
        assert (config.workspace / "workspace.db").exists()
    finally:
        await stop_connectors(session)
