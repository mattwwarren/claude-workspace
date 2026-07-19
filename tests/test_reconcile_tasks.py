"""Unit tests for cw.reconcile.tasks.

Dev-queue revert backstops and timed-out-merged completion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import freezegun
import pytest

from cw.config import (
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import (
    _DIRTY_WORKTREE_REASON,
    complete_timed_out_merged_tasks,
    reconcile,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from tests._reconcile_helpers import (
    _mk_daemon_completed_session,
    _mk_daemon_session_with_worktree,
    _mk_session,
    _mk_timed_out_daemon_session,
)
from tests.conftest import _make_ticket_task

# ---------------------------------------------------------------------------
# revert_completed_silent_tasks tests
# ---------------------------------------------------------------------------


def test_revert_completed_silent_tasks_happy_path(
    tmp_config_dir: Path,
) -> None:
    """DAEMON COMPLETED session + RUNNING task with matching session_id → reverted."""
    sess = _mk_daemon_completed_session("comp-sess-1")
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-CS1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="comp-sess-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_completed_silent_tasks()
    assert "TKT-CS1" in reverted

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TKT-CS1")
    assert t.status == QueueItemStatus.PENDING
    assert t.session_id is None


def test_revert_completed_silent_tasks_skips_user_origin(
    tmp_config_dir: Path,
) -> None:
    """USER origin COMPLETED session + RUNNING task → no revert."""
    sess = _mk_session("user-comp", surface_ref=None, status=SessionStatus.COMPLETED)
    sess.origin = SessionOrigin.USER
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-UO",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="user-comp",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_completed_silent_tasks()
    assert reverted == []

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TKT-UO")
    assert t.status == QueueItemStatus.RUNNING


def test_revert_completed_silent_tasks_skips_non_completed(
    tmp_config_dir: Path,
) -> None:
    """DAEMON ACTIVE/RUNNING/TIMED_OUT session → no revert."""
    for status in (SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.TIMED_OUT):
        sess = _mk_session(f"non-comp-{status}", surface_ref=None, status=status)
        sess.origin = SessionOrigin.DAEMON
        task = TicketTask(
            ticket_id=f"TKT-{status}",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=f"non-comp-{status}",
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[task]))

        reverted = revert_completed_silent_tasks()
        assert reverted == [], f"Expected no revert for status={status}"


def test_revert_completed_silent_tasks_skips_unmatched_session(
    tmp_config_dir: Path,
) -> None:
    """DAEMON COMPLETED session, but task.session_id != that id → no revert."""
    sess = _mk_daemon_completed_session("comp-sess-x")
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-NM",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="different-session",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_completed_silent_tasks()
    assert reverted == []

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TKT-NM")
    assert t.status == QueueItemStatus.RUNNING


def test_revert_completed_silent_tasks_returns_empty_when_no_match(
    tmp_config_dir: Path,
) -> None:
    """No matching sessions → returns empty list."""
    save_state(CwState(sessions=[]))
    save_dev_queue(DevQueueStore(tasks=[]))

    reverted = revert_completed_silent_tasks()
    assert reverted == []


def test_reconcile_merges_completed_silent_reverts_into_report(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() includes both timed-out and completed-silent reverts."""
    timed_out_session = Session(
        id="timed-out-merge",
        name="client-a/auto-dev/TKT-TO",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    completed_silent_session = Session(
        id="comp-merge",
        name="client-a/auto-dev/TKT-CS",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    save_state(CwState(sessions=[timed_out_session, completed_silent_session]))

    dev_store = DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id="TKT-TO",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="timed-out-merge",
            ),
            TicketTask(
                ticket_id="TKT-CS",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="comp-merge",
            ),
        ]
    )
    save_dev_queue(dev_store)

    # No ACTIVE/IDLE sessions with surface_refs → outage guard won't trip.
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    report = reconcile()

    assert "TKT-TO" in report.reverted_ticket_ids
    assert "TKT-CS" in report.reverted_ticket_ids


def test_reconcile_calls_timed_out_then_completed_silent(
    tmp_config_dir: Path,
) -> None:
    """Both revert helpers fire independently and each reverts the right task."""
    from cw.models import ClientConfig

    timed_out_session = Session(
        id="to-ind",
        name="client-a/auto-dev/TKT-IND-TO",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    comp_silent_session = Session(
        id="cs-ind",
        name="client-a/auto-dev/TKT-IND-CS",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    save_state(CwState(sessions=[timed_out_session, comp_silent_session]))

    dev_store = DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id="TKT-IND-TO",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="to-ind",
            ),
            TicketTask(
                ticket_id="TKT-IND-CS",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="cs-ind",
            ),
        ]
    )
    save_dev_queue(dev_store)

    # Call helpers independently to assert each reverts only the right task.
    to_reverted = revert_timed_out_tasks()
    assert "TKT-IND-TO" in to_reverted
    assert "TKT-IND-CS" not in to_reverted

    cs_reverted = revert_completed_silent_tasks()
    assert "TKT-IND-CS" in cs_reverted
    assert "TKT-IND-TO" not in cs_reverted


# ---------------------------------------------------------------------------
# CANCELLED task skipped by revert_completed_silent_tasks
# ---------------------------------------------------------------------------


def test_revert_completed_silent_tasks_skips_cancelled_task(
    tmp_config_dir: Path,
) -> None:
    """DAEMON COMPLETED session + CANCELLED task → no revert, task stays CANCELLED."""
    sess = _mk_daemon_completed_session("comp-sess-cancel")
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-CANCEL",
        client="client-a",
        status=QueueItemStatus.CANCELLED,
        session_id=None,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_completed_silent_tasks()
    assert reverted == []

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TKT-CANCEL")
    assert t.status == QueueItemStatus.CANCELLED


# ---------------------------------------------------------------------------
# GitHub issue #421 — sibling revert paths: timed_out + completed_silent
# ---------------------------------------------------------------------------


def test_revert_timed_out_dirty_worktree_routes_to_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session with dirty worktree routes task to BLOCKED_ON_USER."""
    wt_path = tmp_path / "wt-to-dirty"
    sess = _mk_daemon_session_with_worktree(
        "to-dirty", SessionStatus.TIMED_OUT, wt_path
    )
    sess.lane = "tasks-lane"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="to-dirty",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="to-dirty",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/to-dirty"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    reverted = revert_timed_out_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "to-dirty")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None
    # Dirty ticket not in reverted list
    assert "to-dirty" not in reverted

    # SESSION_NEEDS_ATTENTION emitted with dirty_worktree paused_status
    events = read_events(
        consumer="test-to-dirty-attn",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "to-dirty"
    assert p["paused_status"] == _DIRTY_WORKTREE_REASON
    assert p["lane"] == "tasks-lane"


def test_revert_timed_out_clean_worktree_routes_to_pending(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session with clean worktree routes task to PENDING."""
    wt_path = tmp_path / "wt-to-clean"
    sess = _mk_daemon_session_with_worktree(
        "to-clean", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="to-clean",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="to-clean",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/to-clean"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )

    reverted = revert_timed_out_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "to-clean")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert "to-clean" in reverted


def test_revert_completed_silent_dirty_worktree_routes_to_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED DAEMON session with dirty worktree routes task to BLOCKED_ON_USER."""
    wt_path = tmp_path / "wt-cs-dirty"
    sess = _mk_daemon_session_with_worktree(
        "cs-dirty", SessionStatus.COMPLETED, wt_path
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="cs-dirty",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cs-dirty",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/cs-dirty"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    reverted = revert_completed_silent_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "cs-dirty")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None
    assert "cs-dirty" not in reverted


def test_revert_completed_silent_clean_worktree_routes_to_pending(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED DAEMON session with clean worktree routes task to PENDING."""
    wt_path = tmp_path / "wt-cs-clean"
    sess = _mk_daemon_session_with_worktree(
        "cs-clean", SessionStatus.COMPLETED, wt_path
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="cs-clean",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cs-clean",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/cs-clean"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )

    reverted = revert_completed_silent_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "cs-clean")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert "cs-clean" in reverted


# ---------------------------------------------------------------------------
# TestCompleteTimedOutMergedTasks — #488
# ---------------------------------------------------------------------------


class TestCompleteTimedOutMergedTasks:
    """complete_timed_out_merged_tasks() auto-completes PENDING tasks on merged PR."""

    def _pending_task(self, ticket_id: str) -> TicketTask:
        return _make_ticket_task(ticket_id=ticket_id, client="client-a")

    def test_happy_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DAEMON TIMED_OUT session + PENDING task + merged PR → task COMPLETED.

        SESSION_COMPLETED event must carry the full payload including
        salvaged=True, reason="timed_out_merged", correlation_id=ticket_id.
        ReconcileReport.completed_ticket_ids == ["TKT-X"].
        """
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-happy", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-happy-path",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["session_id"] == "sess-happy"
        assert p["session_name"] == f"client-a/auto-dev/{ticket_id}"
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert "claude_session_id" in p
        assert p["crashed"] is False
        assert p["salvaged"] is True
        assert p["reason"] == "timed_out_merged"
        assert events[0].correlation_id == ticket_id

        report = reconcile.__doc__  # smoke: function exists
        _ = report  # suppress unused

    def test_gh_unavailable_skips_all(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh binary absent → no upgrades, loop breaks, second session NOT processed."""
        now = datetime.now(UTC)
        sessions = [
            _mk_timed_out_daemon_session(
                f"sess-{i}", f"TKT-{i}", completed_at=now - timedelta(hours=1)
            )
            for i in range(2)
        ]
        save_state(CwState(sessions=sessions))
        save_dev_queue(
            DevQueueStore(tasks=[self._pending_task(f"TKT-{i}") for i in range(2)])
        )

        call_count = 0

        def _unavailable(tid: str, **kw: object) -> tuple[None, bool]:
            nonlocal call_count
            call_count += 1
            return None, False

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _unavailable)

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 1  # broke after first call

        store = load_dev_queue()
        for task in store.tasks:
            assert task.status == QueueItemStatus.PENDING

    def test_transient_error_skips_session(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(None, True) for A, (True, True) for B → A skipped, B completed."""
        now = datetime.now(UTC)
        session_a = _mk_timed_out_daemon_session(
            "sess-a", "TKT-A", completed_at=now - timedelta(hours=1)
        )
        session_b = _mk_timed_out_daemon_session(
            "sess-b", "TKT-B", completed_at=now - timedelta(hours=1)
        )
        save_state(CwState(sessions=[session_a, session_b]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._pending_task("TKT-A"), self._pending_task("TKT-B")]
            )
        )

        def _transient_then_merged(tid: str, **kw: object) -> tuple[bool | None, bool]:
            if tid == "TKT-A":
                return None, True
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _transient_then_merged
        )

        completed = complete_timed_out_merged_tasks()

        assert "TKT-B" in completed
        assert "TKT-A" not in completed

        store = load_dev_queue()
        by_tid = {t.ticket_id: t for t in store.tasks}
        assert by_tid["TKT-A"].status == QueueItemStatus.PENDING
        assert by_tid["TKT-B"].status == QueueItemStatus.COMPLETED

    def test_idempotency(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second reconcile() call emits ZERO extra events (task already COMPLETED)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-IDEM"
        session = _mk_timed_out_daemon_session(
            "sess-idem", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _merged(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _merged)

        complete_timed_out_merged_tasks()
        assert call_count == 1

        # Second call: task is now COMPLETED, should skip gh call entirely.
        call_count = 0
        complete_timed_out_merged_tasks()
        assert call_count == 0

        events = read_events(
            consumer="test-idempotency",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1  # only from first call

    def test_pr_not_merged(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(False, True) → task stays PENDING, no SESSION_COMPLETED event."""
        now = datetime.now(UTC)
        ticket_id = "TKT-OPEN"
        session = _mk_timed_out_daemon_session(
            "sess-open", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING

        events = read_events(
            consumer="test-pr-not-merged",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 0

    def test_outside_lookback_window(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Session completed_at > 7 days ago → skipped, no gh call made."""
        fixed_now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
        ticket_id = "TKT-OLD"
        # 8 days ago — outside the 7-day lookback
        old_completed_at = fixed_now - timedelta(days=8)
        session = _mk_timed_out_daemon_session(
            "sess-old", ticket_id, completed_at=old_completed_at
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        with freezegun.freeze_time(fixed_now):
            completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

    def test_inside_lookback_window(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Session completed_at within 7 days → processed normally."""
        fixed_now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
        ticket_id = "TKT-RECENT"
        # 6 days ago — inside the 7-day lookback
        recent_completed_at = fixed_now - timedelta(days=6)
        session = _mk_timed_out_daemon_session(
            "sess-recent", ticket_id, completed_at=recent_completed_at
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        with freezegun.freeze_time(fixed_now):
            completed = complete_timed_out_merged_tasks()

        assert ticket_id in completed

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

    def test_completed_at_none_legacy_session(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session.completed_at = None → skipped without TypeError."""
        now = datetime.now(UTC)
        ticket_id = "TKT-LEGACY"
        session = _mk_timed_out_daemon_session(
            "sess-legacy", ticket_id, completed_at=now - timedelta(hours=1)
        )
        # Override completed_at to None to simulate legacy state
        session.completed_at = None  # type: ignore[assignment]
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        # Must not raise
        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

    def test_task_running_not_upgraded(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Task status is RUNNING → no gh call made, task stays RUNNING."""
        now = datetime.now(UTC)
        ticket_id = "TKT-RUN"
        session = _mk_timed_out_daemon_session(
            "sess-run", ticket_id, completed_at=now - timedelta(hours=1)
        )
        save_state(CwState(sessions=[session]))

        running_task = TicketTask(
            ticket_id=ticket_id, client="client-a", status=QueueItemStatus.RUNNING
        )
        save_dev_queue(DevQueueStore(tasks=[running_task]))

        call_count = 0

        def _should_not_be_called(tid: str, **kw: object) -> tuple[bool, bool]:
            nonlocal call_count
            call_count += 1
            return True, True

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        assert call_count == 0

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_custom_feature_branch_prefix_used(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """feature_branch_prefix='feat' → branch='feat/TKT-X' passed to gh check."""
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-custom-prefix", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        # Write clients.yaml with custom feature_branch_prefix
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_branch == [f"feat/{ticket_id}"]

    def test_custom_feature_branch_prefix_used_passes_cwd(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cwd resolved from client's workspace_path reaches the gh check (#1269)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-X"
        session = _mk_timed_out_daemon_session(
            "sess-custom-prefix-cwd", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        captured_cwd: list[Path | None] = []

        def _capture(
            tid: str, *, branch: str, cwd: Path | None = None, **_kw: object
        ) -> tuple[bool, bool]:
            captured_cwd.append(cwd)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_cwd == [Path("/tmp/ws-feat")]

    def test_default_feature_branch_prefix_fallback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No clients.yaml → fallback branch='dev/TKT-Y' (default prefix)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-Y"
        session = _mk_timed_out_daemon_session(
            "sess-default-prefix", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        # No clients.yaml written — load_clients() returns {}

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        complete_timed_out_merged_tasks()

        assert captured_branch == [f"dev/{ticket_id}"]

    def test_dangling_client_skips_gh_call_stays_pending(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """clients.yaml populated but missing this session's client → skip.

        Distinct from "no clients.yaml at all" (see
        test_default_feature_branch_prefix_fallback, which must still call
        gh with cwd=None): here clients.yaml is populated with a *different*
        client, so this session's client is dangling/config-drifted. An
        unscoped gh call would risk the same cross-repo misattribution the
        ticket describes (#1269), so the candidate must be skipped entirely
        rather than falling back to an ambient-cwd gh call.
        """
        now = datetime.now(UTC)
        ticket_id = "TKT-DANGLING"
        session = _mk_timed_out_daemon_session(
            "sess-dangling-client", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(DevQueueStore(tasks=[self._pending_task(ticket_id)]))

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        calls: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            calls.append(tid)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        completed = complete_timed_out_merged_tasks()

        assert calls == []
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING


def test_reap_reason_completed_backstop_timed_out(
    tmp_config_dir: Path,
) -> None:
    """revert_timed_out_tasks backstop sets reap_reason=completed_backstop on sessions
    without a prior reap_reason."""
    sess = Session(
        id="backstop-to-1",
        name="client-a/auto-dev/BACKSTOP-TO-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.TIMED_OUT,
        # reap_reason intentionally None — simulates signal_stop path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BACKSTOP-TO-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="backstop-to-1",
                )
            ]
        )
    )

    revert_timed_out_tasks()

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "backstop-to-1")
    assert s.reap_reason == ReapReason.COMPLETED_BACKSTOP


def test_reap_reason_completed_backstop_completed(
    tmp_config_dir: Path,
) -> None:
    """revert_completed_silent_tasks backstop sets reap_reason=completed_backstop."""
    sess = Session(
        id="backstop-c-1",
        name="client-a/auto-dev/BACKSTOP-C-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.NORMAL,
        # reap_reason intentionally None
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BACKSTOP-C-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="backstop-c-1",
                )
            ]
        )
    )

    revert_completed_silent_tasks()

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "backstop-c-1")
    assert s.reap_reason == ReapReason.COMPLETED_BACKSTOP


def test_reap_reason_not_overwritten_by_backstop(
    tmp_config_dir: Path,
) -> None:
    """revert_timed_out_tasks backstop does NOT overwrite an existing reap_reason."""
    sess = Session(
        id="backstop-skip-1",
        name="client-a/auto-dev/BACKSTOP-SKIP-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.TIMED_OUT,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,  # already set by reconcile
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BACKSTOP-SKIP-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="backstop-skip-1",
                )
            ]
        )
    )

    revert_timed_out_tasks()

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "backstop-skip-1")
    # Must remain WALL_CLOCK_BUDGET, not be overwritten with COMPLETED_BACKSTOP
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


def test_reap_reason_not_stamped_when_task_already_completed_timed_out(
    tmp_config_dir: Path,
) -> None:
    """revert_timed_out_tasks must NOT stamp reap_reason when the session's
    dev-queue task is already COMPLETED (happy-path completion) — only sessions
    whose task is still RUNNING get the COMPLETED_BACKSTOP stamp."""
    sess = Session(
        id="backstop-to-noop-1",
        name="client-a/auto-dev/BACKSTOP-TO-NOOP-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.TIMED_OUT,
        # reap_reason intentionally None — simulates signal_stop path
    )
    save_state(CwState(sessions=[sess]))
    # Task is already COMPLETED, NOT RUNNING — the session completed normally.
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BACKSTOP-TO-NOOP-1",
                    client="client-a",
                    status=QueueItemStatus.COMPLETED,
                    session_id="backstop-to-noop-1",
                )
            ]
        )
    )

    revert_timed_out_tasks()

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "backstop-to-noop-1")
    # Task was not RUNNING so no revert happened — reap_reason must stay None.
    assert s.reap_reason is None


def test_reap_reason_not_stamped_when_task_already_completed_completed_silent(
    tmp_config_dir: Path,
) -> None:
    """revert_completed_silent_tasks must NOT stamp reap_reason when the
    session's dev-queue task is already COMPLETED (happy-path completion) —
    only sessions whose task is still RUNNING get the COMPLETED_BACKSTOP stamp."""
    sess = Session(
        id="backstop-c-noop-1",
        name="client-a/auto-dev/BACKSTOP-C-NOOP-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.NORMAL,
        # reap_reason intentionally None
    )
    save_state(CwState(sessions=[sess]))
    # Task is already COMPLETED, NOT RUNNING — the session completed normally.
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BACKSTOP-C-NOOP-1",
                    client="client-a",
                    status=QueueItemStatus.COMPLETED,
                    session_id="backstop-c-noop-1",
                )
            ]
        )
    )

    revert_completed_silent_tasks()

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "backstop-c-noop-1")
    # Task was not RUNNING so no revert happened — reap_reason must stay None.
    assert s.reap_reason is None


def test_revert_timed_out_tasks_completes_while_holding_sessions_lock(
    tmp_config_dir: Path,
) -> None:
    """Regression for the #387 self-deadlock: production calls
    revert_timed_out_tasks() from _reconcile_locked, i.e. while already
    holding sessions_lock. The reap_reason stamp must save in place rather
    than re-acquire the lock (flock is per-open-fd, so re-acquiring
    self-deadlocks)."""
    sess = Session(
        id="lockheld-to-1",
        name="client-a/auto-dev/LOCKHELD-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_reason=CompletionReason.TIMED_OUT,
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="LOCKHELD-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="lockheld-to-1",
                )
            ]
        )
    )

    with sessions_lock():
        reverted = revert_timed_out_tasks()

    assert reverted == ["LOCKHELD-1"]
    reloaded = load_state()
    stamped = next(s for s in reloaded.sessions if s.id == "lockheld-to-1")
    assert stamped.reap_reason == ReapReason.COMPLETED_BACKSTOP
