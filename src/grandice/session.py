"""Session state and the audit log.

Every tool call is logged with its arguments (§08.5). When something goes wrong
you need the transcript, and you need it queryable — JSONL plus jq covers P0;
SQLite arrives with resumable tasks in P1.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config
from .permissions import Gate
from .router import Router
from .sandbox import Sandbox
from .skills import SkillMeta
from .tasks import TaskStore
from .tools import Registry
from .tools.todo import TodoList

if TYPE_CHECKING:
    from .mcp_client import Connector


@dataclass
class Session:
    config: Config
    router: Router
    sandbox: Sandbox
    registry: Registry
    gate: Gate
    tasks: TaskStore
    todos: TodoList = field(default_factory=TodoList)
    skills: list[SkillMeta] = field(default_factory=list)
    connectors: "list[Connector]" = field(default_factory=list)

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    turns: int = 0
    cancelled: bool = False
    compactions: int = 0

    # tool name -> consecutive failures, for the reflection trigger (§05.8)
    failures: dict[str, int] = field(default_factory=dict)

    @property
    def log_path(self) -> Path:
        path = self.config.workspace.parent / ".grandice" / f"{self.id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def overflow_dir(self) -> Path:
        return self.config.workspace / ".grandice-overflow"

    def log(self, kind: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "session": self.id, "kind": kind, **fields}
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def note_failure(self, tool: str) -> int:
        self.failures[tool] = self.failures.get(tool, 0) + 1
        return self.failures[tool]

    def note_success(self, tool: str) -> None:
        self.failures.pop(tool, None)


def build(config: Config, gate: Gate) -> Session:
    """Synchronous — constructs everything, but does not start any
    configured MCP connectors. Those need a running event loop (the MCP SDK
    is async throughout), so call `await start_connectors(session)` once
    inside one before the first turn; see cli.py and server/app.py."""
    from . import sandbox as sandbox_mod
    from . import skills as skills_mod
    from .tools import build_registry

    box = sandbox_mod.build(config.sandbox, config.workspace, image=config.sandbox_image)
    skills_dir = skills_mod.sync_into_workspace(config.workspace)
    discovered = skills_mod.discover(skills_dir)

    connectors: list[Connector] = []
    if config.mcp_connectors:
        from . import mcp_client

        connectors = [
            mcp_client.Connector(mcp_client.spec_for(name, config.workspace, config.mcp_sqlite_path))
            for name in config.mcp_connectors
        ]

    # Shared across every session against this workspace, not per-session —
    # a background task spawned in one run should still show up in
    # list_tasks after a restart (§P4).
    tasks = TaskStore(config.workspace.parent / ".grandice" / "tasks.db")

    todos = TodoList()
    registry = build_registry(box, todos, skills=discovered)
    session = Session(
        config=config,
        router=Router(config),
        sandbox=box,
        registry=registry,
        gate=gate,
        tasks=tasks,
        todos=todos,
        skills=discovered,
        connectors=connectors,
    )

    # The subagent tools close over `session` itself, so they're added once
    # it exists — same reasoning as search_tools needing the registry.
    from .tools import subagent as subagent_tool

    for spec in subagent_tool.build(session):
        registry.add(spec)

    return session


async def start_connectors(session: Session) -> None:
    """No-op with the default empty connector list — safe to call always."""
    if not session.connectors:
        return
    from . import mcp_client

    await mcp_client.start_connectors(session.connectors, session.registry)
    session.log("connectors_started", names=[c.spec.name for c in session.connectors])


async def stop_connectors(session: Session) -> None:
    if not session.connectors:
        return
    from . import mcp_client

    await mcp_client.stop_connectors(session.connectors)
