"""Tests for projects and chats (§projects.py). The security-critical
property this whole feature exists to guarantee is cross-user isolation —
one user must never be able to read, write, rename, or delete another
user's project or chat, and a nonexistent id must look identical to one
that exists but belongs to someone else."""

from __future__ import annotations

from pathlib import Path

import pytest

from grandice.projects import NotFound, ProjectStore


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    s = ProjectStore(tmp_path / "projects.db")
    yield s
    s.close()


def test_create_and_list_projects(store: ProjectStore):
    store.create_project("u1", "Website redesign")
    store.create_project("u1", "Data pipeline")
    names = {p.name for p in store.list_projects("u1")}
    assert names == {"Website redesign", "Data pipeline"}


def test_list_projects_only_returns_this_users_projects(store: ProjectStore):
    store.create_project("u1", "mine")
    store.create_project("u2", "theirs")
    assert [p.name for p in store.list_projects("u1")] == ["mine"]


def test_get_project_raises_not_found_for_another_users_project(store: ProjectStore):
    p = store.create_project("u1", "mine")
    with pytest.raises(NotFound):
        store.get_project(p.id, "u2")


def test_get_project_raises_not_found_for_a_nonexistent_id_the_same_way(store: ProjectStore):
    p = store.create_project("u1", "mine")

    # A real project belonging to someone else, and an id that was never
    # issued at all, must both fail with the same exception type (the id
    # itself isn't secret — the caller already supplied it — but nothing
    # about *why* it failed should be distinguishable beyond that).
    with pytest.raises(NotFound):
        store.get_project(p.id, "u2")
    with pytest.raises(NotFound):
        store.get_project("does-not-exist", "u2")


def test_delete_project_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    with pytest.raises(NotFound):
        store.delete_project(p.id, "u2")
    assert store.get_project(p.id, "u1") is not None  # untouched


def test_delete_project_cascades_to_its_chats(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")

    store.delete_project(p.id, "u1")

    with pytest.raises(NotFound):
        store.get_chat(c.id, "u1")


def test_create_chat_is_blocked_in_another_users_project(store: ProjectStore):
    p = store.create_project("u1", "mine")
    with pytest.raises(NotFound):
        store.create_chat(p.id, "u2")


def test_list_chats_only_returns_this_users_chats(store: ProjectStore):
    p = store.create_project("u1", "mine")
    store.create_chat(p.id, "u1", "chat a")
    with pytest.raises(NotFound):
        store.list_chats(p.id, "u2")


def test_get_chat_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    with pytest.raises(NotFound):
        store.get_chat(c.id, "u2")


def test_load_messages_defaults_to_empty(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    assert store.load_messages(c.id, "u1") == []


def test_save_then_load_messages_round_trips(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    store.save_messages(c.id, "u1", messages)

    assert store.load_messages(c.id, "u1") == messages


def test_save_messages_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    with pytest.raises(NotFound):
        store.save_messages(c.id, "u2", [{"role": "user", "content": "sneaky"}])
    assert store.load_messages(c.id, "u1") == []  # untouched


def test_rename_chat(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1", "old title")
    store.rename_chat(c.id, "u1", "new title")
    assert store.get_chat(c.id, "u1").title == "new title"


def test_rename_chat_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1", "old title")
    with pytest.raises(NotFound):
        store.rename_chat(c.id, "u2", "hijacked")
    assert store.get_chat(c.id, "u1").title == "old title"


def test_delete_chat_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    with pytest.raises(NotFound):
        store.delete_chat(c.id, "u2")
    assert store.get_chat(c.id, "u1") is not None


def test_new_chat_has_no_model_override_by_default(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    assert c.model is None
    assert store.get_chat(c.id, "u1").model is None


def test_set_chat_model(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    store.set_chat_model(c.id, "u1", "code")
    assert store.get_chat(c.id, "u1").model == "code"


def test_set_chat_model_to_none_clears_the_override(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    store.set_chat_model(c.id, "u1", "code")
    store.set_chat_model(c.id, "u1", None)
    assert store.get_chat(c.id, "u1").model is None


def test_set_chat_model_is_blocked_for_a_non_owner(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    with pytest.raises(NotFound):
        store.set_chat_model(c.id, "u2", "code")
    assert store.get_chat(c.id, "u1").model is None


def test_list_chats_includes_the_model_override(store: ProjectStore):
    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")
    store.set_chat_model(c.id, "u1", "vision")
    assert store.list_chats(p.id, "u1")[0].model == "vision"


def test_existing_chats_db_without_a_model_column_migrates_cleanly(tmp_path: Path):
    """A real pre-existing chats.db (from before the model selector existed)
    must not break on open — ALTER TABLE ADD COLUMN runs once, by hand,
    since CREATE TABLE IF NOT EXISTS is a no-op on an already-existing
    table."""
    import sqlite3

    db_path = tmp_path / "old_projects.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE chats (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT NOT NULL,
            messages_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'u1', 'old project', 0)"
    )
    conn.execute(
        "INSERT INTO chats VALUES ('c1', 'p1', 'u1', 'old chat', '[]', 0, 0)"
    )
    conn.commit()
    conn.close()

    store = ProjectStore(db_path)
    chat = store.get_chat("c1", "u1")
    assert chat.title == "old chat"
    assert chat.model is None  # migrated column defaults to NULL
    store.set_chat_model("c1", "u1", "chat")
    assert store.get_chat("c1", "u1").model == "chat"
    store.close()


# --- concurrency: same reasoning as accounts.py's identical fix — most of
# these methods run inside route handlers FastAPI resolves via a thread
# pool, so this one shared sqlite3.Connection is genuinely used from
# multiple threads at once on real traffic, not just hypothetically -------

def test_get_chat_survives_real_concurrent_access(store: ProjectStore):
    import threading

    p = store.create_project("u1", "mine")
    c = store.create_chat(p.id, "u1")

    errors: list[BaseException] = []
    results: list[object] = []

    def worker():
        try:
            results.append(store.get_chat(c.id, "u1"))
        except BaseException as exc:  # noqa: BLE001 — swallowing this would hide the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 50
    assert all(r.id == c.id for r in results)


def test_delete_project_reentrant_lock_does_not_deadlock(store: ProjectStore):
    """delete_project calls get_project (also locking) while already
    holding the lock — this only works because the lock is reentrant
    (RLock); a plain Lock would deadlock the very first call. Run with a
    timeout via a background thread so a real deadlock fails the test
    instead of hanging the suite forever."""
    import threading

    p = store.create_project("u1", "mine")
    store.create_chat(p.id, "u1")

    done = threading.Event()

    def worker():
        store.delete_project(p.id, "u1")
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=2)
    assert done.is_set(), "delete_project deadlocked — the lock must be reentrant"
