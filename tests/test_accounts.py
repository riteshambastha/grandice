"""Tests for real authentication (§accounts.py): bcrypt password hashing,
random session tokens stored only as a hash, and the same-error-message
anti-enumeration behavior for both registration collisions and login
failures."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from grandice.accounts import AccountStore, InvalidCredentials, UsernameTaken


@pytest.fixture
def store(tmp_path: Path) -> AccountStore:
    s = AccountStore(tmp_path / "accounts.db")
    yield s
    s.close()


def test_register_then_authenticate_round_trips(store: AccountStore):
    user = store.register("alice", "correct horse")
    assert user.username == "alice"

    authed = store.authenticate("alice", "correct horse")
    assert authed.id == user.id


def test_register_rejects_a_duplicate_username(store: AccountStore):
    store.register("alice", "correct horse")
    with pytest.raises(UsernameTaken):
        store.register("alice", "another password")


def test_register_rejects_a_short_password(store: AccountStore):
    with pytest.raises(ValueError):
        store.register("bob", "short")


def test_register_rejects_an_empty_username(store: AccountStore):
    with pytest.raises(ValueError):
        store.register("   ", "long enough password")


def test_password_is_never_stored_in_plaintext(store: AccountStore, tmp_path: Path):
    store.register("alice", "correct horse battery staple")
    raw = (tmp_path / "accounts.db").read_bytes()
    assert b"correct horse battery staple" not in raw


def test_authenticate_rejects_wrong_password(store: AccountStore):
    store.register("alice", "correct horse")
    with pytest.raises(InvalidCredentials):
        store.authenticate("alice", "wrong password")


def test_authenticate_rejects_unknown_user_with_the_same_error(store: AccountStore):
    store.register("alice", "correct horse")

    def _message(username: str, password: str) -> str:
        try:
            store.authenticate(username, password)
        except InvalidCredentials as exc:
            return str(exc)
        raise AssertionError("expected InvalidCredentials")

    # A wrong password for a real user and a login for a nonexistent one
    # must be indistinguishable — otherwise a request can enumerate
    # registered usernames by the error text alone.
    assert _message("alice", "wrong password") == _message("nobody", "whatever")


def test_create_session_returns_a_token_that_resolves_to_the_user(store: AccountStore):
    user = store.register("alice", "correct horse")
    token = store.create_session(user.id)

    resolved = store.resolve_session(token)
    assert resolved is not None
    assert resolved.id == user.id


def test_only_the_tokens_hash_is_stored(store: AccountStore, tmp_path: Path):
    user = store.register("alice", "correct horse")
    token = store.create_session(user.id)
    raw = (tmp_path / "accounts.db").read_bytes()
    assert token.encode() not in raw


def test_resolve_session_returns_none_for_a_garbage_token(store: AccountStore):
    assert store.resolve_session("not-a-real-token") is None


def test_delete_session_logs_out(store: AccountStore):
    user = store.register("alice", "correct horse")
    token = store.create_session(user.id)
    store.delete_session(token)
    assert store.resolve_session(token) is None


def test_resolve_session_rejects_an_expired_token(store: AccountStore, monkeypatch):
    import grandice.accounts as accounts_mod

    monkeypatch.setattr(accounts_mod, "SESSION_LIFETIME_SECONDS", 0.05)
    user = store.register("alice", "correct horse")
    token = store.create_session(user.id)

    time.sleep(0.1)
    assert store.resolve_session(token) is None


# --- concurrency: FastAPI resolves current_user via a thread pool, so this
# one shared sqlite3.Connection genuinely gets hit from multiple threads at
# once on every real request, not just hypothetically -----------------------

def test_resolve_session_survives_real_concurrent_access(store: AccountStore):
    """Real bug, caught live: enough concurrent resolve_session() calls on
    one connection (no lock) produced both a TypeError (expires_at read
    back as None) and a raw sqlite3.InterfaceError, from two threads'
    queries interleaving on the same connection/cursor state."""
    import threading

    user = store.register("alice", "correct horse")
    token = store.create_session(user.id)

    errors: list[BaseException] = []
    results: list[object] = []

    def worker():
        try:
            results.append(store.resolve_session(token))
        except BaseException as exc:  # noqa: BLE001 — a thread swallowing this would hide the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 50
    assert all(r is not None and r.id == user.id for r in results)
