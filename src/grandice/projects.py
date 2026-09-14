"""Projects and chats.

A project owns one workspace/sandbox directory — team members working in
different projects never share a filesystem. Nested one level (each
project's real sandbox root is <project_dir>/workspace, not <project_dir>
itself) so that .grandice/tasks.db (the background-task queue, §P4), which
already lives at workspace.parent/.grandice, lands inside that project's
own directory rather than a shared projects root shared across everyone —
no code change to session.py/tasks.py needed for that isolation; it falls
out of where the workspace path points.

A chat is one persisted conversation — its full message history (the same
list run_turn already appends to) saved as JSON after each turn, so
reopening a chat actually resumes it. That's a stronger guarantee than
tasks.py's background-task records: those survive a restart but not the
in-progress conversation itself; a chat's whole point is that it does.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CHAT_TITLE = "New chat"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT
);
"""

_CHAT_COLUMNS = "id, project_id, user_id, title, created_at, updated_at, model"


@dataclass
class Project:
    id: str
    user_id: str
    name: str
    created_at: float


@dataclass
class Chat:
    id: str
    project_id: str
    user_id: str
    title: str
    created_at: float
    updated_at: float
    # None means "use the session's configured default model" — the model
    # selector's own choice for this chat, if the user ever picked one.
    model: str | None = None


class NotFound(ValueError):
    """A project/chat id that either doesn't exist, or exists but belongs
    to a different user — deliberately the same error for both, so a
    request can never distinguish "doesn't exist" from "isn't yours"."""


class ProjectStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS doesn't add a column to a table that
        # already existed before this one was added (a real, pre-existing
        # chats.db from before the model selector) — add it by hand, once.
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(chats)")}
        if "model" not in existing:
            self._conn.execute("ALTER TABLE chats ADD COLUMN model TEXT")
        self._conn.commit()
        # Real bug, caught live in accounts.py's identical pattern (see its
        # own comment): this one shared sqlite3.Connection is genuinely
        # used from multiple threads at once, since most of these methods
        # run inside route handlers FastAPI resolves via a thread pool.
        # check_same_thread=False alone doesn't make that safe. RLock, not
        # a plain Lock: several methods here call another locking method on
        # `self` while already holding the lock (delete_project calling
        # get_project, create_chat calling get_project, and so on) — a
        # plain Lock would deadlock on that first nested call.
        self._lock = threading.RLock()

    # --- projects -----------------------------------------------------

    def create_project(self, user_id: str, name: str) -> Project:
        name = name.strip() or "Untitled project"
        project_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects (id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                (project_id, user_id, name, now),
            )
            self._conn.commit()
        return Project(id=project_id, user_id=user_id, name=name, created_at=now)

    def list_projects(self, user_id: str) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [Project(**dict(r)) for r in rows]

    def get_project(self, project_id: str, user_id: str) -> Project:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
            ).fetchone()
        if row is None:
            raise NotFound(f"No project {project_id!r} for this user.")
        return Project(**dict(row))

    def delete_project(self, project_id: str, user_id: str) -> None:
        with self._lock:
            self.get_project(project_id, user_id)  # raises NotFound if not this user's
            self._conn.execute("DELETE FROM chats WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            self._conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))
            self._conn.commit()

    # --- chats ----------------------------------------------------------

    def create_chat(self, project_id: str, user_id: str, title: str = DEFAULT_CHAT_TITLE) -> Chat:
        chat_id = uuid.uuid4().hex[:12]
        now = time.time()
        title = title.strip() or DEFAULT_CHAT_TITLE
        with self._lock:
            self.get_project(project_id, user_id)  # raises NotFound if not this user's project
            self._conn.execute(
                "INSERT INTO chats (id, project_id, user_id, title, messages_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '[]', ?, ?)",
                (chat_id, project_id, user_id, title, now, now),
            )
            self._conn.commit()
        return Chat(id=chat_id, project_id=project_id, user_id=user_id,
                    title=title, created_at=now, updated_at=now, model=None)

    def list_chats(self, project_id: str, user_id: str) -> list[Chat]:
        with self._lock:
            self.get_project(project_id, user_id)
            rows = self._conn.execute(
                f"SELECT {_CHAT_COLUMNS} FROM chats "
                "WHERE project_id = ? AND user_id = ? ORDER BY updated_at DESC",
                (project_id, user_id),
            ).fetchall()
        return [Chat(**dict(r)) for r in rows]

    def get_chat(self, chat_id: str, user_id: str) -> Chat:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CHAT_COLUMNS} FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFound(f"No chat {chat_id!r} for this user.")
        return Chat(**dict(row))

    def set_chat_model(self, chat_id: str, user_id: str, model: str | None) -> None:
        """None clears the override, reverting the chat to the session's
        configured default model."""
        with self._lock:
            self.get_chat(chat_id, user_id)
            self._conn.execute(
                "UPDATE chats SET model = ?, updated_at = ? WHERE id = ?",
                (model, time.time(), chat_id),
            )
            self._conn.commit()

    def load_messages(self, chat_id: str, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self.get_chat(chat_id, user_id)
            row = self._conn.execute("SELECT messages_json FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return json.loads(row["messages_json"])

    def save_messages(self, chat_id: str, user_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self.get_chat(chat_id, user_id)  # keeps the write authorization-checked too
            self._conn.execute(
                "UPDATE chats SET messages_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages, default=str), time.time(), chat_id),
            )
            self._conn.commit()

    def rename_chat(self, chat_id: str, user_id: str, title: str) -> None:
        with self._lock:
            self.get_chat(chat_id, user_id)
            self._conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip() or DEFAULT_CHAT_TITLE, time.time(), chat_id),
            )
            self._conn.commit()

    def delete_chat(self, chat_id: str, user_id: str) -> None:
        with self._lock:
            self.get_chat(chat_id, user_id)
            self._conn.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
