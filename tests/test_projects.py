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
