"""Focused regression coverage for atomic conditional native task claim."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_claim_hooks():
    manager = get_plugin_manager()
    events: list[dict] = []
    saved = {name: list(callbacks) for name, callbacks in manager._hooks.items()}
    manager._hooks.setdefault("kanban_task_claimed", []).append(
        lambda **kwargs: events.append(kwargs)
    )
    try:
        yield events
    finally:
        manager._hooks = saved


def _run_count(conn, task_id):
    return conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0]


def _event_count(conn, task_id):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()[0]


def test_existing_claim_api_and_concurrency_are_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ordinary", assignee="alpha")
        assert kb.claim_task(conn, task_id, claimer="ordinary") is not None

        race_id = kb.create_task(conn, title="ordinary race", assignee="alpha")

    def claim(claimer):
        with kb.connect() as conn:
            return kb.claim_task(conn, race_id, claimer=claimer) is not None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(claim, ("ordinary-a", "ordinary-b")))
    assert sum(winners) == 1


def test_conditional_claim_success_creates_normal_run_event_hook_and_no_dispatch(
    kanban_home, captured_claim_hooks,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="conditional", assignee="alpha")
        observed = kb.task_claim_snapshot(conn, task_id)
        result = kb.claim_task_if_current(
            conn, task_id, observed, claimer="conditional", ttl_seconds=37,
        )

        assert result.outcome == "accepted"
        assert result.task is not None
        assert result.task.status == "running"
        assert result.task.claim_lock == "conditional"
        assert result.task.claim_expires is not None
        assert result.task.current_run_id == result.run_id
        assert result.snapshot != observed
        assert result.task.worker_pid is None  # claim remains non-dispatching
        assert _run_count(conn, task_id) == 1
        claimed_events = [e for e in kb.list_events(conn, task_id) if e.kind == "claimed"]
        assert len(claimed_events) == 1
        run = conn.execute(
            "SELECT profile, claim_lock, claim_expires, status FROM task_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert tuple(run) == ("alpha", "conditional", result.task.claim_expires, "running")

    assert len(captured_claim_hooks) == 1
    assert captured_claim_hooks[0]["task_id"] == task_id
    assert captured_claim_hooks[0]["run_id"] == result.run_id


def test_stale_assignee_and_status_snapshots_conflict_without_side_effects(kanban_home):
    with kb.connect() as conn:
        assignee_id = kb.create_task(conn, title="assignee", assignee="alpha")
        stale_assignee = kb.task_claim_snapshot(conn, assignee_id)
        assert kb.assign_task(conn, assignee_id, "beta") is True
        runs_before = _run_count(conn, assignee_id)
        events_before = _event_count(conn, assignee_id)
        result = kb.claim_task_if_current(conn, assignee_id, stale_assignee)
        current = kb.get_task(conn, assignee_id)
        assert result.outcome == "conflict"
        assert result.task == current
        assert current.status == "ready"
        assert current.assignee == "beta"
        assert _run_count(conn, assignee_id) == runs_before
        assert _event_count(conn, assignee_id) == events_before

        status_id = kb.create_task(conn, title="status", assignee="alpha")
        stale_status = kb.task_claim_snapshot(conn, status_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (status_id,))
        result = kb.claim_task_if_current(conn, status_id, stale_status)
        assert result.outcome == "conflict"
        assert kb.get_task(conn, status_id).status == "todo"


def test_stale_claim_run_and_same_snapshot_race_are_exactly_once(kanban_home):
    with kb.connect() as conn:
        stale_id = kb.create_task(conn, title="stale run", assignee="alpha")
        stale = kb.task_claim_snapshot(conn, stale_id)
        assert kb.claim_task(conn, stale_id, claimer="other") is not None
        result = kb.claim_task_if_current(conn, stale_id, stale)
        assert result.outcome == "conflict"
        assert _run_count(conn, stale_id) == 1

        race_id = kb.create_task(conn, title="same snapshot", assignee="alpha")
        observed = kb.task_claim_snapshot(conn, race_id)

    def conditional(claimer):
        with kb.connect() as conn:
            return kb.claim_task_if_current(conn, race_id, observed, claimer=claimer).outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(conditional, ("first", "second")))
    assert sorted(outcomes) == ["accepted", "conflict"]

    with kb.connect() as conn:
        assert _run_count(conn, race_id) == 1
        assert len([e for e in kb.list_events(conn, race_id) if e.kind == "claimed"]) == 1


def test_conditional_claim_not_found_and_snapshot_cannot_cross_task_or_board(kanban_home):
    with kb.connect() as conn:
        assert kb.claim_task_if_current(conn, "missing", "sha256:missing").outcome == "not_found"
        first = kb.create_task(conn, title="first", assignee="alpha")
        second = kb.create_task(conn, title="second", assignee="alpha")
        foreign_snapshot = kb.task_claim_snapshot(conn, first)
        result = kb.claim_task_if_current(conn, second, foreign_snapshot)
        assert result.outcome == "conflict"
        assert kb.get_task(conn, second).status == "ready"

        kb.create_board("other")
        with kb.connect(board="other") as other:
            other_id = kb.create_task(other, title="other", assignee="alpha")
            other_snapshot = kb.task_claim_snapshot(other, other_id)
        result = kb.claim_task_if_current(conn, second, other_snapshot)
        assert result.outcome == "conflict"


def test_disposable_board_proof_reassignment_then_fresh_conditional_claim(kanban_home):
    """Real-board HCC-001 proof: stale alpha rejects, fresh beta accepts."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="real board proof", assignee="alpha")
        r1 = kb.task_claim_snapshot(conn, task_id)
        assert kb.assign_task(conn, task_id, "beta") is True
        stale = kb.claim_task_if_current(conn, task_id, r1, claimer="stale")
        assert stale.outcome == "conflict"
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_task(conn, task_id).assignee == "beta"

        r2 = kb.task_claim_snapshot(conn, task_id)
        fresh = kb.claim_task_if_current(conn, task_id, r2, claimer="fresh")
        assert fresh.outcome == "accepted"
        assert fresh.task.status == "running"
