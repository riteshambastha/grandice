"""Tests for the SQLite-backed task store (§P4)."""

from __future__ import annotations

from pathlib import Path

from grandice.tasks import TaskStatus, TaskStore


def test_create_starts_pending(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    task_id = store.create("subagent", "do the thing")
    task = store.get(task_id)
    assert task.status is TaskStatus.PENDING
    assert task.description == "do the thing"
    store.close()


def test_status_transitions(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    task_id = store.create("subagent", "do the thing")

    store.mark_running(task_id)
    assert store.get(task_id).status is TaskStatus.RUNNING

    store.mark_done(task_id, "the result")
    task = store.get(task_id)
    assert task.status is TaskStatus.DONE
    assert task.result == "the result"
    store.close()


def test_mark_failed_keeps_the_error(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    task_id = store.create("subagent", "do the thing")
    store.mark_failed(task_id, "boom")
    task = store.get(task_id)
    assert task.status is TaskStatus.FAILED
    assert task.error == "boom"
    store.close()


def test_list_returns_newest_first(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    first = store.create("subagent", "first")
    second = store.create("subagent", "second")
    ids = [t.id for t in store.list()]
    assert ids == [second, first]
    store.close()


def test_get_on_unknown_id_returns_none(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    assert store.get("nonexistent") is None
    store.close()


def test_a_running_task_survives_a_restart_as_interrupted(tmp_path: Path):
    """The actual scope of "resumable": the record survives a crash, marked
    honestly rather than silently lost or falsely shown as still running."""
    db_path = tmp_path / "tasks.db"

    first = TaskStore(db_path)
    task_id = first.create("subagent", "long running thing")
    first.mark_running(task_id)
    first.close()  # simulates the process dying mid-task, no clean shutdown

    second = TaskStore(db_path)  # simulates a restart
    assert second.get(task_id).status is TaskStatus.INTERRUPTED
    second.close()


def test_a_done_task_is_unaffected_by_reconciliation(tmp_path: Path):
    db_path = tmp_path / "tasks.db"
    first = TaskStore(db_path)
    task_id = first.create("subagent", "quick thing")
    first.mark_running(task_id)
    first.mark_done(task_id, "finished fine")
    first.close()

    second = TaskStore(db_path)
    task = second.get(task_id)
    assert task.status is TaskStatus.DONE  # reconciliation only touches "running"
    assert task.result == "finished fine"
    second.close()
