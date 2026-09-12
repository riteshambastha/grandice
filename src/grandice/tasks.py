"""Background task tracking (§P4). SQLite — §09's own stack pick for this:
"Sessions, messages, tasks, cost ledger. Postgres is premature." One shared
file per workspace, at .grandice/tasks.db (same directory as the per-session
JSONL audit logs), using stdlib sqlite3 — no new dependency.

"Resumable" is scoped honestly here, not oversold: a task's *record*
(status, description, result/error) survives a process restart, and one
caught mid-run when the process dies is marked `interrupted` on the next
startup rather than silently vanishing. It does NOT mean the exact
in-progress subagent conversation is transparently continued — that would
need checkpointing the subagent's message list at every step, which isn't
built. "Resuming" an interrupted task today means re-running its original
description from scratch, with full knowledge that it didn't finish last
time — not picking back up mid-transcript.
"""

from __future__ import annotations

import enum
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # was "running" when this store was last opened


@dataclass
class Task:
    id: str
    kind: str
    description: str
    status: TaskStatus
    created_at: float
    updated_at: float
    result: str | None = None
    error: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result TEXT,
    error TEXT
)
"""


class TaskStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the CLI/dashboard call this from whatever
        # thread asyncio happens to schedule a background task on; sqlite3's
        # own serialization (one connection, no concurrent writers we don't
        # control) is enough here, there's no real multi-threaded contention.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        self._reconcile_interrupted()

    def _reconcile_interrupted(self) -> None:
        """Anything still "running" when this store was last open didn't
        finish — the process died mid-task. Mark it rather than pretend."""
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE status = ?",
            (TaskStatus.INTERRUPTED.value, time.time(), TaskStatus.RUNNING.value),
        )
        self._conn.commit()

    def create(self, kind: str, description: str) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO tasks (id, kind, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, kind, description, TaskStatus.PENDING.value, now, now),
        )
        self._conn.commit()
        return task_id

    def mark_running(self, task_id: str) -> None:
        self._set(task_id, TaskStatus.RUNNING)

    def mark_done(self, task_id: str, result: str) -> None:
        self._set(task_id, TaskStatus.DONE, result=result)

    def mark_failed(self, task_id: str, error: str) -> None:
        self._set(task_id, TaskStatus.FAILED, error=error)

    def _set(
        self, task_id: str, status: TaskStatus, result: str | None = None, error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, "
            "result = COALESCE(?, result), error = COALESCE(?, error) WHERE id = ?",
            (status.value, time.time(), result, error, task_id),
        )
        self._conn.commit()

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [self._row_to_task(row) for row in rows]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            kind=row["kind"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=row["result"],
            error=row["error"],
        )

    def close(self) -> None:
        self._conn.close()
