"""User accounts and session authentication.

Real authentication, not a profile picker: bcrypt-hashed passwords, random
session tokens stored only as a hash (the same principle as never storing a
plaintext password — a stolen database row shouldn't hand over a live
session), cookie-based sessions with an expiry. A small team, each seeing
only their own projects and chats, is what actually requires this; a plain
"pick a name" switcher would have been enough for one person alone.

SQLite, one file per install (~/.grandice/accounts.db by default) — not
per-project, since accounts span every project. This is dashboard/desktop-
app only: the plain `grandice` CLI stays the single-user, no-login tool it
always was.

A real limitation worth stating plainly, not glossing over: this protects
credentials at the login layer, but if the dashboard is reached over plain
HTTP on a shared network, the password is still visible in transit to
anyone on that network path. Put it behind HTTPS (a reverse proxy) or a VPN
if it's genuinely reachable by more than one trusted machine — this module
does not solve transport security, only who's-who once a request arrives.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import bcrypt

SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 days
MIN_PASSWORD_LENGTH = 8


@dataclass
class User:
    id: str
    username: str
    created_at: float


class UsernameTaken(ValueError):
    pass


class InvalidCredentials(ValueError):
    """Deliberately the same error for "no such user" and "wrong password"
    — telling the two apart lets an attacker enumerate real usernames."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def _hash_token(token: str) -> str:
    """Session tokens are already high-entropy random values, not
    human-chosen secrets — sha256 is the right tool here. bcrypt is
    deliberately slow to resist guessing a *password*; applied to a token
    that's already unguessable, that slowness would just tax every
    authenticated request for no security benefit."""
    return hashlib.sha256(token.encode()).hexdigest()


class AccountStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Real bug, caught live: FastAPI resolves the `current_user`
        # dependency (which calls resolve_session on every authenticated
        # request) via a thread pool, so this one shared sqlite3.Connection
        # is genuinely accessed from multiple threads at once —
        # check_same_thread=False only disables Python's own same-thread
        # check, it does not make concurrent use of one connection safe.
        # Confirmed directly: enough concurrent requests produced both a
        # TypeError (expires_at read back as None) and a raw
        # sqlite3.InterfaceError, from two threads' queries interleaving on
        # the same connection/cursor state. A single lock around every
        # query serializes access without needing a connection per thread.
        self._lock = threading.Lock()

    def register(self, username: str, password: str) -> User:
        username = username.strip()
        if not username:
            raise ValueError("Username must not be empty.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        with self._lock:
            if self._conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise UsernameTaken(f"{username!r} is already taken.")

            user_id = uuid.uuid4().hex[:12]
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
            now = time.time()
            self._conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, password_hash, now),
            )
            self._conn.commit()
        return User(id=user_id, username=username, created_at=now)

    def authenticate(self, username: str, password: str) -> User:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username.strip(),)
            ).fetchone()
        if row is None or not bcrypt.checkpw(password.encode(), row["password_hash"]):
            raise InvalidCredentials("Incorrect username or password.")
        return User(id=row["id"], username=row["username"], created_at=row["created_at"])

    def create_session(self, user_id: str) -> str:
        """Returns the *raw* token — this is the only place it ever exists
        outside the client's cookie; only its hash is ever stored."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_hash_token(token), user_id, now, now + SESSION_LIFETIME_SECONDS),
            )
            self._conn.commit()
        return token

    def resolve_session(self, token: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT sessions.user_id, sessions.expires_at, users.username, users.created_at "
                "FROM sessions JOIN users ON sessions.user_id = users.id "
                "WHERE sessions.token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return User(id=row["user_id"], username=row["username"], created_at=row["created_at"])

    def delete_session(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
