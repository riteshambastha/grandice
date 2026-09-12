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
from typing import Any

from .config import Config
from .permissions import Gate
from .router import Router
from .sandbox import Sandbox
from .tools import Registry
from .tools.todo import TodoList


@dataclass
class Session:
    config: Config
    router: Router
    sandbox: Sandbox
    registry: Registry
    gate: Gate
    todos: TodoList = field(default_factory=TodoList)

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
    from . import sandbox as sandbox_mod
    from .tools import build_registry

    box = sandbox_mod.build(config.sandbox, config.workspace)
    todos = TodoList()
    return Session(
        config=config,
        router=Router(config),
        sandbox=box,
        registry=build_registry(box, todos),
        gate=gate,
        todos=todos,
    )
