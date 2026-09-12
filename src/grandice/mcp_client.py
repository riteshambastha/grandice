"""MCP connectors (§07's "Connectors: Drive, Slack, mail, databases — via MCP
servers", §09's "Do not hand-roll a protocol client"). Wraps the official
`mcp` SDK; imported lazily so a build with no connectors configured never
needs the `mcp` package installed at all.

Connectors attach at the tool layer, never inside the sandbox (Fig. 1 in the
build spec) — a connector subprocess runs alongside the harness with its own
network access, deliberately outside the sandbox's egress-denied profile.
`fetch` is the reason that distinction exists: it needs real internet access
to do its one job.

`mcp` is pinned to the 1.x line (see pyproject.toml), not chosen loosely.
Verified live (2026-09-12): `mcp` 2.x renamed `Tool.inputSchema` to
`input_schema` and `CallToolResult.isError` to `is_error` — and
mcp-server-fetch (even its latest release at the time) still imports
`McpError` from `mcp.shared.exceptions`, a name 2.x removed outright, so it
cannot run on 2.x at all. mcp-server-sqlite (last released 2025.4.25) is
similarly built against the 1.x API. Both work correctly against 1.x, which
is why this ships pinned there rather than tracking 2.x — re-verify field
names directly against whatever's actually installed before ever bumping
this pin; the fast pace of change here is the whole reason this note exists.

Optional install: `pip install -e ".[mcp]"`.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.base import Risk, ToolSpec


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)


def _venv_bin(executable_name: str) -> str:
    """Resolve a console script next to the harness's own interpreter — the
    same reasoning as sandbox._clean_env's PATH fix: whichever venv installed
    grandice is the one with mcp-server-fetch/mcp-server-sqlite installed."""
    return str(Path(sys.executable).parent / executable_name)


def spec_for(name: str, workspace: Path, sqlite_path: str) -> ConnectorSpec:
    """The two connectors this build ships: the community mcp-server-fetch
    and mcp-server-sqlite packages. Command/args verified by actually
    starting each server and listing its tools (2026-09-12), not assumed
    from package docs — `python -m mcp_server_sqlite` in particular does not
    work; it has no `__main__`, only a console-script entry point."""
    if name == "fetch":
        return ConnectorSpec(name="fetch", command=_venv_bin("mcp-server-fetch"))
    if name == "sqlite":
        db_path = workspace / sqlite_path
        return ConnectorSpec(
            name="sqlite", command=_venv_bin("mcp-server-sqlite"),
            args=["--db-path", str(db_path)],
        )
    raise ValueError(f"Unknown connector {name!r}. Known: fetch, sqlite.")


# Verified against each server's real list_tools() response (2026-09-12).
# MCP carries no risk metadata of its own, so this is where that judgment
# lives: fetch leaves the machine (Risk.OUTWARD, gated per §08); sqlite's
# reads and writes are both local and confined to one file, same trust level
# as the built-in read/write tools. Unknown future tools default to WRITE —
# not READ, since an unverified tool's side effects are exactly what's
# unverified.
_RISK_OVERRIDES: dict[tuple[str, str], Risk] = {
    ("fetch", "fetch"): Risk.OUTWARD,
    ("sqlite", "read_query"): Risk.READ,
    ("sqlite", "list_tables"): Risk.READ,
    ("sqlite", "describe_table"): Risk.READ,
    ("sqlite", "write_query"): Risk.WRITE,
    ("sqlite", "create_table"): Risk.WRITE,
    ("sqlite", "append_insight"): Risk.WRITE,
}

# mcp-server-sqlite's confirmed behaviour: a failed query comes back with
# isError=False and the failure embedded in the text ("Database error: ...").
# MCP's own isError flag is the primary signal; this is a best-effort
# fallback verified against this one server, not a general solution — a
# different connector may fail differently, and its error handling should be
# checked before assuming this catches it.
_ERROR_TEXT_SIGNALS = ("database error:", "sqlite error:")


class ConnectorError(RuntimeError):
    """A connector's tool call failed — surfaced to the model as a normal
    tool error (§04: every failure returns to the model as a tool result)."""


class Connector:
    """One running MCP server subprocess and the session talking to it."""

    def __init__(self, spec: ConnectorSpec) -> None:
        self.spec = spec
        self._stack: AsyncExitStack | None = None
        self._session: Any = None  # mcp.ClientSession, typed loosely to keep the import lazy

    async def start(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self.spec.command, args=self.spec.args)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def tool_specs(self) -> list[ToolSpec]:
        result = await self._session.list_tools()
        specs = []
        for tool in result.tools:
            specs.append(
                ToolSpec(
                    name=f"{self.spec.name}.{tool.name}",
                    description=f"[{self.spec.name} connector] {tool.description or tool.name}",
                    schema=tool.inputSchema,
                    run=self._make_run(tool.name),
                    risk=_RISK_OVERRIDES.get((self.spec.name, tool.name), Risk.WRITE),
                )
            )
        return specs

    def _make_run(self, mcp_tool_name: str):
        async def run(**arguments: Any) -> str:
            from mcp.types import TextContent

            result = await self._session.call_tool(mcp_tool_name, arguments)
            text = "\n".join(
                block.text for block in result.content if isinstance(block, TextContent)
            ) or "(no text content returned)"

            failed = result.isError or any(
                signal in text.lower() for signal in _ERROR_TEXT_SIGNALS
            )
            if failed:
                raise ConnectorError(text)
            return text

        return run


async def start_connectors(connectors: list[Connector], registry) -> None:
    """Start each configured connector and register its tools as latent
    (§05.2) — invisible to the model until search_tools activates them."""
    for connector in connectors:
        await connector.start()
        for spec in await connector.tool_specs():
            registry.add(spec, active=False)


async def stop_connectors(connectors: list[Connector]) -> None:
    """Best-effort: one connector failing to shut down cleanly (e.g. its
    subprocess already died) must not stop the rest from being cleaned up."""
    for connector in connectors:
        try:
            await connector.stop()
        except Exception:  # noqa: BLE001 — shutdown must not be interrupted by this
            pass
