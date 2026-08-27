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
from cw.events import read_events, record_event
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
    WatchedPr,
)
from cw.pr_hydrate import hydrate_pr_states
from cw.reconcile import (
    _DIRTY_WORKTREE_REASON,
    _NEVER_CLAIMED_COMPLETION_REASON,
    complete_timed_out_merged_tasks,
    reconcile,
    release_stale_gated_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.reconcile.stale_dispatch_watch import register_stale_dispatch_watched_prs
from cw.reconcile.tasks import _merged_pr_numbers_by_client
from tests._reconcile_helpers import (
    _client_with_lane,
    _mk_daemon_completed_session,
    _mk_daemon_session_with_worktree,
    _mk_session,
    _mk_timed_out_daemon_session,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

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
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b, **_kw: True
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
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b, **_kw: False
    )

    reverted = revert_timed_out_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "to-clean")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert "to-clean" in reverted


def test_revert_timed_out_does_not_touch_regressed_into_stage(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1801: reap's revert-to-PENDING path never clobbers whatever value is
    in regressed_into_stage -- it only mutates status/session_id. This pins
    the invariant so a future change to the field's clearing timing can rely
    on reap being a safe no-op, independent of when upstream (claim.py)
    actually clears it today."""
    wt_path = tmp_path / "wt-to-clean-marker"
    sess = _mk_daemon_session_with_worktree(
        "to-clean-marker", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="to-clean-marker",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="to-clean-marker",
        regressed_into_stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/to-clean-marker"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b, **_kw: False
    )

    reverted = revert_timed_out_tasks()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "to-clean-marker")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert updated_task.regressed_into_stage == Stage.IMPL
    assert "to-clean-marker" in reverted


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
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b, **_kw: True
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
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b, **_kw: False
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
        """PENDING task with a real claim history (attempts=1, session_id
        already cleared) -- the legitimate shape this backstop targets.
        Distinct from _never_claimed_task (#1387)."""
        return _make_ticket_task(ticket_id=ticket_id, client="client-a", attempts=1)

    def _never_claimed_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=0,
            session_id=None,
            lane=lane,
        )

    def _spawn_error_only_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        """1617's exact observed shape: every attempt died on the generic
        spawn-error path. attempts == spawn_error_count == 3, session_id=None,
        stage_high_water=None (never entered a stage)."""
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.PLAN,
            stage_high_water=None,
            attempts=3,
            spawn_error_count=3,
            session_id=None,
            signoff="operator",
            lane=lane,
        )

    def _usage_limit_only_task(
        self, ticket_id: str, *, lane: str = "batch-2"
    ) -> TicketTask:
        """#1631's exact shape: every attempt died via UsageLimitError.

        That revert path calls _revert_claimed_task_to_pending with
        stamp_backoff=False (dispatch/claim.py), so the counters move
        asymmetrically -- `attempts` increments at claim time but
        `spawn_error_count` never does. The row is therefore
        attempts=3, spawn_error_count=0, session_id=None,
        stage_high_water=None: indistinguishable from a legitimate
        first-stage ship on the pre-#1631 predicate, and only
        ever_spawned=False separates the two.
        """
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.PLAN,
            stage_high_water=None,
            attempts=3,
            spawn_error_count=0,
            session_id=None,
            ever_spawned=False,
            lane=lane,
        )

    def _legitimate_first_stage_task(self, ticket_id: str) -> TicketTask:
        """A task that genuinely spawned, ran and shipped inside its FIRST
        pipeline stage, then timed out before the row advanced.

        stage_high_water is None and spawn_error_count is 0, matching
        _usage_limit_only_task above -- and both tasks' `attempts` values
        (1 here vs. 3 there) equally fail the first disjunct's
        `attempts == spawn_error_count` check against spawn_error_count=0, so
        neither `attempts` nor `stage` (which the guard does not read at all)
        affects _is_never_claimed's verdict here; only `ever_spawned` does.
        This is the shape #1623's reverted stage_high_water disjunct wrongly
        refused; the test using it exists to prove #1631's new predicate does
        NOT reintroduce that regression.
        """
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.IMPL,
            stage_high_water=None,
            attempts=1,
            spawn_error_count=0,
            session_id=None,
            ever_spawned=True,
        )

    def _legitimately_progressed_reverted_task(self, ticket_id: str) -> TicketTask:
        """A task that genuinely ran, completed at least one stage, then was
        reverted to PENDING (session_id cleared) for retry -- attempts >
        spawn_error_count and stage_high_water is set. Must NOT be refused;
        this is the failure mode that would make the fix worse than the bug."""
        return _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.IMPL,
            stage_high_water=Stage.IMPL,
            attempts=2,
            spawn_error_count=0,
            session_id=None,
        )

    def test_never_claimed_row_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """attempts=0, session_id=None row matching a timed-out-merged
        candidate is refused (#1387 belt-and-braces guard): stays PENDING,
        is NOT in the return value, no SESSION_COMPLETED fires, and exactly
        one SESSION_NEEDS_ATTENTION fires with client/ticket_id/lane/paused_status."""
        now = datetime.now(UTC)
        ticket_id = "TKT-NEVER-CLAIMED"
        session = _mk_timed_out_daemon_session(
            "sess-never-claimed", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(tasks=[self._never_claimed_task(ticket_id, lane="batch-2")])
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 0
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-never-claimed-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-never-claimed-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id

    def test_spawn_error_only_history_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """1617's exact observed shape: attempts=3, spawn_error_count=3,
        session_id=None, stage_high_water=None -- every attempt died on the
        generic spawn-error path. Refused: stays PENDING, NOT in the return
        value, no SESSION_COMPLETED fires, exactly one SESSION_NEEDS_ATTENTION
        fires with client/ticket_id/lane/paused_status."""
        now = datetime.now(UTC)
        ticket_id = "TKT-SPAWN-ERROR"
        session = _mk_timed_out_daemon_session(
            "sess-spawn-error", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._spawn_error_only_task(ticket_id, lane="batch-2")]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 3
        assert task.spawn_error_count == 3
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-spawn-error-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-spawn-error-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id

    def test_usage_limit_only_history_refused_stays_pending_and_emits_needs_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1631's exact shape: attempts=3, spawn_error_count=0,
        session_id=None, stage_high_water=None, ever_spawned=False -- every
        attempt died via UsageLimitError, whose revert deliberately does NOT
        stamp spawn_error_count. Refused: stays PENDING, NOT in the return
        value, no SESSION_COMPLETED fires, exactly one SESSION_NEEDS_ATTENTION
        fires whose breadcrumb names the never-spawned cause rather than the
        (wrong, for this shape) spawn-error-path cause."""
        now = datetime.now(UTC)
        ticket_id = "TKT-USAGE-LIMIT"
        session = _mk_timed_out_daemon_session(
            "sess-usage-limit", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._usage_limit_only_task(ticket_id, lane="batch-2")]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.attempts == 3
        assert task.spawn_error_count == 0
        assert task.session_id is None

        completed_events = read_events(
            consumer="test-usage-limit-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(completed_events) == 0

        attention_events = read_events(
            consumer="test-usage-limit-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(attention_events) == 1
        p = attention_events[0].payload
        assert p["client"] == "client-a"
        assert p["ticket_id"] == ticket_id
        assert p["lane"] == "batch-2"
        assert p["paused_status"] == _NEVER_CLAIMED_COMPLETION_REASON
        assert attention_events[0].correlation_id == ticket_id
        breadcrumbs = p["breadcrumbs"]
        assert "never successfully spawned" in breadcrumbs
        assert "ever_spawned=False" in breadcrumbs
        assert "died on the spawn-error path" not in breadcrumbs

    def test_legitimate_first_stage_ship_still_completes(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Criterion preservation (#1623 R4, #1631): a task that genuinely
        spawned and shipped inside its first pipeline stage -- attempts=1,
        spawn_error_count=0, stage_high_water=None, ever_spawned=True -- is
        still completed. The only thing separating it from the usage-limit
        shape above is ever_spawned, so a predicate that refused this one
        would be worse than the bug it closes."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FIRST-STAGE"
        session = _mk_timed_out_daemon_session(
            "sess-first-stage", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(tasks=[self._legitimate_first_stage_task(ticket_id)])
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"

        events = read_events(
            consumer="test-first-stage-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload["reason"] == "timed_out_merged"

    def test_legitimately_progressed_reverted_task_still_completes(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A task that genuinely ran, then was reverted to PENDING for retry
        (attempts > spawn_error_count) must NOT be refused -- proves the
        widened predicate doesn't over-refuse legitimate recoveries."""
        now = datetime.now(UTC)
        ticket_id = "TKT-LEGIT-RETRY"
        session = _mk_timed_out_daemon_session(
            "sess-legit-retry", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._legitimately_progressed_reverted_task(ticket_id)]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"

        events = read_events(
            consumer="test-legit-retry-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload["reason"] == "timed_out_merged"

    def test_complete_timed_out_merged_tasks_forces_finalize_stage_not_high_water(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1629: a salvage completion forces stage to FINALIZE so the row
        stops advertising a stage it never finished, while stage_high_water
        stays where it was -- the divergence IS the salvage marker."""
        now = datetime.now(UTC)
        ticket_id = "TKT-SALVAGE-STAGE"
        session = _mk_timed_out_daemon_session(
            "sess-salvage-stage", ticket_id, completed_at=now - timedelta(days=1)
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[self._legitimately_progressed_reverted_task(ticket_id)]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        assert complete_timed_out_merged_tasks() == [ticket_id]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.disposition == "shipped"
        assert task.stage == Stage.FINALIZE
        assert task.stage_high_water == Stage.IMPL

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

    def test_fresh_merged_pr_state_completes_without_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: a fresh MERGED pr_state on the task auto-completes it
        without any _deps.pr_is_merged_for_ticket call."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FRESH-MERGED"
        session = _mk_timed_out_daemon_session(
            "sess-fresh-merged", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="MERGED", hydrated_at=now - timedelta(seconds=10)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == [ticket_id]
        store = load_dev_queue()
        completed_task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert completed_task.status == QueueItemStatus.COMPLETED

    def test_fresh_non_merged_pr_state_stays_pending_without_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: fresh non-MERGED pr_state -> stays PENDING, no gh call."""
        now = datetime.now(UTC)
        ticket_id = "TKT-FRESH-OPEN"
        session = _mk_timed_out_daemon_session(
            "sess-fresh-open", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="OPEN", hydrated_at=now - timedelta(seconds=10)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []
        store = load_dev_queue()
        pending_task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert pending_task.status == QueueItemStatus.PENDING

    def test_stale_pr_state_falls_back_to_gh_call(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #975: stale pr_state falls back to the ordinary gh call
        path unchanged (pre-#975 behavior)."""
        now = datetime.now(UTC)
        ticket_id = "TKT-STALE-STATE"
        session = _mk_timed_out_daemon_session(
            "sess-stale-state", ticket_id, completed_at=now - timedelta(hours=2)
        )
        save_state(CwState(sessions=[session]))
        task = _make_ticket_task(
            ticket_id=ticket_id,
            client="client-a",
            attempts=1,
            pr_state=PrState(state="MERGED", hydrated_at=now - timedelta(seconds=300)),
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _capture(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
            calls.append(_tid)
            return True, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)

        completed = complete_timed_out_merged_tasks()

        assert calls == [ticket_id]
        assert completed == [ticket_id]

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

    def test_does_not_complete_different_clients_same_ticket(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """client-a's TIMED_OUT merged session must not complete client-b's
        identically-numbered PENDING task (#1385) -- mirrors the #1054 fix in
        core.py's merged_client_ticket_ids scoping, applied here to
        complete_timed_out_merged_tasks's own (client, ticket_id) matching."""
        now = datetime.now(UTC)
        ticket_id = "1"
        session = _make_daemon_session(
            name="client-a/auto-dev/1",
            client="client-a",
            status=SessionStatus.TIMED_OUT,
            completed_at=now - timedelta(days=1),
        )
        client_b_task = _make_ticket_task(ticket_id=ticket_id, client="client-b")
        save_state(CwState(sessions=[session]))
        # Intentionally no client-a queue row -- reproduces the bug even
        # without a same-client task present.
        save_dev_queue(DevQueueStore(tasks=[client_b_task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        completed = complete_timed_out_merged_tasks()

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING

        events = read_events(
            consumer="test-does-not-complete-different-clients",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 0


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


# ---------------------------------------------------------------------------
# release_stale_gated_tasks (GitHub #1713)
#
# Conftest inventory (binding operator resolution #3): reuses
# tests.conftest._make_ticket_task for every TicketTask construction and
# tests._reconcile_helpers._client_with_lane for the lane-override test. No
# hand-rolled builders introduced.
# ---------------------------------------------------------------------------


def _emit_pr_merged(*, client: str, ticket_id: str, repo: str, pr_number: int) -> None:
    """Emit a PR_MERGED event matching apply_pr_state_observation's payload
    shape (pr_hydrate.py's ``base`` dict: {repo, pr_number, ticket_id, client})."""
    record_event(
        OrchestratorEventType.PR_MERGED,
        {
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": ticket_id,
            "client": client,
        },
        correlation_id=ticket_id,
    )


def test_release_stale_gated_tasks_signal_only_stamps_and_emits(
    tmp_config_dir: Path,
) -> None:
    """Variant A (merge_pending, own PR merged) under default signal_only:
    status unchanged, stale_gate_detected_at stamped, SESSION_REAP_PROPOSED
    emitted with reason=STALE_GATE, proposed_action=variant_a."""
    task = _make_ticket_task(
        ticket_id="SG-A1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/42",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A1", repo="foo/bar", pr_number=42)

    released = release_stale_gated_tasks()

    assert released == ["SG-A1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A1")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["reason"] == ReapReason.STALE_GATE.value
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_a"


def test_release_stale_gated_tasks_automerge_not_armed_via_pr_info_fallback(
    tmp_config_dir: Path,
) -> None:
    """Variant A detection fires identically for an automerge_not_armed park
    (disposition='blocked', blocked_reason='automerge_not_armed') whose
    pr_url was already populated via dispatch/routing.py's pr_info fallback
    (tested separately in test_dispatch.py)."""
    task = _make_ticket_task(
        ticket_id="SG-A2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="blocked",
        blocked_reason="automerge_not_armed",
        pr_url="https://github.com/foo/bar/pull/43",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A2", repo="foo/bar", pr_number=43)

    released = release_stale_gated_tasks()

    assert released == ["SG-A2"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A2")
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER


def test_release_stale_gated_tasks_auto_policy_completes_variant_a(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: Variant A completes to shipped and still emits
    SESSION_REAP_PROPOSED -- the event fires regardless of policy."""
    task = _make_ticket_task(
        ticket_id="SG-A3",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/44",
        session_id="sess-a3",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )
    _emit_pr_merged(client="client-a", ticket_id="SG-A3", repo="foo/bar", pr_number=44)

    released = release_stale_gated_tasks()

    assert released == ["SG-A3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A3")
    assert reloaded.status == QueueItemStatus.COMPLETED
    assert reloaded.disposition == "shipped"
    assert reloaded.session_id is None

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A3"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_a"


def test_release_stale_gated_tasks_auto_policy_requeues_variant_b(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: Variant B (blocked behind another ticket's PR that
    has since merged) requeues to PENDING, clears session_id, still emits
    SESSION_REAP_PROPOSED."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKER",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/50",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=50,
        session_id="sess-b1",
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-B1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B1")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-B1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


# GitHub #1862/#1902 — the verbatim agent sentinel shape a stale_dispatch park
# is routed from; mirrors TestStaleDispatchSentinelRouting._last_result() in
# tests/test_dispatch.py so the two stay recognizably the same payload.
_STALE_DISPATCH_LAST_RESULT: dict[str, object] = {
    "status": "stale_dispatch",
    "blocker": {
        "stage": "stage1_pre_flight",
        "reason": "pr_already_open",
        "details": "PR #1899 (dev/1862) is open and awaiting review.",
    },
    "commits": [],
    "review": {"must_fix_initial": 0, "should_fix": 0},
}
_STALE_DISPATCH_PR_NUMBER = 1899
_STALE_DISPATCH_SLUG = "foo/bar"


def _park_stale_dispatch_via_routing(
    ticket_id: str, client: str, workspace: Path
) -> TicketTask:
    """Park a RUNNING row through the REAL #1902 routing path and persist it.

    No field is hand-set: ``apply_staged_decision`` stamps status,
    disposition, blocked_reason, and ``blocked_on_pr`` exactly as production
    does when an agent reports a ``stale_dispatch`` sentinel.
    """
    from cw.dispatch import apply_staged_decision

    task = _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        stage=Stage.IMPL,
        session_id=f"sess-{ticket_id}",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    apply_staged_decision(
        task,
        "stale_dispatch",
        dict(_STALE_DISPATCH_LAST_RESULT),
        {client: ClientConfig(name=client, workspace_path=workspace)},
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    return task


def _register_and_hydrate_merged_watch(
    monkeypatch: pytest.MonkeyPatch, *, state: str = "MERGED"
) -> None:
    """Run the two real production passes that give the park a PR-state source.

    ``register_stale_dispatch_watched_prs`` is called as a STANDALONE step
    (not inline with the stamping above), so what this exercises is the
    retroactive per-tick rescan of an already-parked row — binding A2's
    "a park stamped before this feature existed must still be registered".
    ``hydrate_pr_states`` then fills the watch's ``pr_state`` through the same
    ``_derive_pr_state`` seam a real serve tick uses.
    """
    monkeypatch.setattr(
        "cw.reconcile.stale_dispatch_watch._resolve_repo_slug",
        lambda _git_dir: _STALE_DISPATCH_SLUG,
    )
    register_stale_dispatch_watched_prs()
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
    monkeypatch.setattr(
        "cw.pr_hydrate.fetch_pr_view",
        lambda *_a, **_kw: {
            "state": state,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
            "reviewDecision": "",
            "isDraft": False,
            "reviewRequests": [],
            "comments": [],
        },
    )
    hydrate_pr_states(OrchestratorConfig())
    # Pin what makes these tests load-bearing rather than incidental: the
    # blocking PR's state reaches the release scan ONLY through the watch --
    # no task row in the store carries that pr_url for the task-scan half of
    # _merged_pr_numbers_by_client to find.
    store = load_dev_queue()
    assert all(t.pr_url is None for t in store.tasks)
    assert len(store.watched_prs) == 1
    assert store.watched_prs[0].pr_number == _STALE_DISPATCH_PR_NUMBER
    assert store.watched_prs[0].pr_state is not None
    assert store.watched_prs[0].pr_state.state == state


def test_release_stale_gated_tasks_auto_policy_requeues_stale_dispatch_variant_b(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reap_policy=auto: a stale_dispatch/pr_already_open park (the agent-
    reported GitHub #1862 sentinel, extended per #1902) requeues to PENDING
    once its own blocking PR is observed MERGED.

    End-to-end through the production path #1927 closed: the row is parked by
    ``apply_staged_decision`` (not hand-built), the blocking PR's state comes
    from a ``WatchedPr`` registered by a standalone retroactive rescan and
    hydrated by the real ``hydrate_pr_states`` pass, and no other task row
    carries that PR anywhere in the store.
    """
    _park_stale_dispatch_via_routing("SG-SD1", "client-a", tmp_path)
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch)
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    released = release_stale_gated_tasks()

    assert released == ["SG-SD1"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD1")
    assert reloaded.status == QueueItemStatus.PENDING
    assert reloaded.session_id is None
    assert reloaded.blocked_on_pr is None  # unconditional-clear on transition

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-SD1"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["proposed_action"] == "release_stale_gate_variant_b"


def test_release_stale_gated_tasks_stale_dispatch_open_pr_not_released_end_to_end(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same production path, blocking PR still OPEN: no release, no stamp.

    The no-false-release half of the #1927 contract — proves the new
    ``WatchedPr`` source releases on MERGED specifically, not merely on the
    watch existing.
    """
    _park_stale_dispatch_via_routing("SG-SD-OPEN", "client-a", tmp_path)
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch, state="OPEN")
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    assert release_stale_gated_tasks() == []

    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD-OPEN")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == _STALE_DISPATCH_PR_NUMBER
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_stale_dispatch_still_open_pr_is_noop(
    tmp_config_dir: Path,
) -> None:
    """A stale_dispatch park whose blocking PR is still OPEN (not yet
    merged) must not be released -- the ticket's explicit no-false-release
    case (#1902). Narrow predicate-edge fixture: hand-built rows keep this
    test focused on the cross-reference predicate itself; the end-to-end
    production path is covered by
    test_release_stale_gated_tasks_stale_dispatch_open_pr_not_released_end_to_end."""
    blocking = _make_ticket_task(
        ticket_id="SG-SD-BLOCKER2",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_url="https://github.com/foo/bar/pull/71",
        pr_state=PrState(state="OPEN"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-SD2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="stale_dispatch",
        blocked_reason="pr_already_open",
        blocked_on_pr=71,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD2")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == 71


def test_release_stale_gated_tasks_stale_dispatch_signal_only_stamps(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default signal_only (ADR-0006): the same end-to-end production path as
    the AUTO test above stamps ``stale_gate_detected_at`` and leaves
    ``status`` untouched -- no destructive mutation without an opt-in."""
    _park_stale_dispatch_via_routing("SG-SD3", "client-a", tmp_path)
    save_state(CwState(sessions=[]))
    _register_and_hydrate_merged_watch(monkeypatch)

    released = release_stale_gated_tasks()

    assert released == ["SG-SD3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD3")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == _STALE_DISPATCH_PR_NUMBER


def test_is_variant_b_gate_task_stale_dispatch_requires_matching_blocked_reason(
    tmp_config_dir: Path,
) -> None:
    """Guards the predicate's second `and`: a stale_dispatch-disposition row
    with a mismatched blocked_reason must not be released, even with a
    non-null blocked_on_pr and a merged blocking PR available in the
    cross-reference index. Narrow predicate-edge fixture -- hand-built rows
    are deliberate here (the production park always carries the matching
    reason, so it cannot produce this row)."""
    blocking = _make_ticket_task(
        ticket_id="SG-SD-BLOCKER4",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/73",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-SD4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="stale_dispatch",
        blocked_reason="some_other_reason",
        blocked_on_pr=73,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-SD4")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.blocked_on_pr == 73


class TestMergedPrNumbersByClient:
    """The Variant B cross-reference index, now fed by two sources (#1927):
    task rows' hydrated ``pr_state`` and client-tagged ``WatchedPr`` entries.
    """

    def _watched(
        self,
        *,
        pr_number: int = 90,
        client: str | None = "client-a",
        state: str = "MERGED",
    ) -> WatchedPr:
        return WatchedPr(
            pr_url=f"https://github.com/foo/bar/pull/{pr_number}",
            repo="foo/bar",
            pr_number=pr_number,
            client=client,
            source="stale_dispatch_park",
            pr_state=PrState(state=state),
        )

    def test_merged_client_tagged_watch_contributes(self) -> None:
        store = DevQueueStore(watched_prs=[self._watched()])
        assert _merged_pr_numbers_by_client(store) == {"client-a": {90}}

    def test_unmerged_watch_excluded(self) -> None:
        store = DevQueueStore(watched_prs=[self._watched(state="OPEN")])
        assert _merged_pr_numbers_by_client(store) == {}

    def test_watch_without_pr_state_excluded(self) -> None:
        watched = self._watched()
        watched.pr_state = None
        assert _merged_pr_numbers_by_client(DevQueueStore(watched_prs=[watched])) == {}

    def test_null_client_watch_excluded(self) -> None:
        """An operator-registered (webhook/cli) watch carries no client
        context, so its bare PR number cannot be scoped to one repo."""
        store = DevQueueStore(watched_prs=[self._watched(client=None)])
        assert _merged_pr_numbers_by_client(store) == {}

    def test_other_client_watch_does_not_leak(self) -> None:
        store = DevQueueStore(watched_prs=[self._watched(client="client-b")])
        merged = _merged_pr_numbers_by_client(store)
        assert merged.get("client-a", set()) == set()
        assert merged == {"client-b": {90}}

    def test_task_and_watch_sources_union_within_a_client(self) -> None:
        task = _make_ticket_task(
            ticket_id="SG-U1",
            client="client-a",
            pr_url="https://github.com/foo/bar/pull/91",
            pr_state=PrState(state="MERGED"),
        )
        store = DevQueueStore(tasks=[task], watched_prs=[self._watched()])
        assert _merged_pr_numbers_by_client(store) == {"client-a": {90, 91}}


def test_release_stale_gated_tasks_variant_b_signal_only_stamps(
    tmp_config_dir: Path,
) -> None:
    """Default signal_only: Variant B stamps stale_gate_detected_at without
    mutating status, symmetric with Variant A's signal_only test."""
    blocking = _make_ticket_task(
        ticket_id="SG-B-BLOCKER2",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="https://github.com/foo/bar/pull/60",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B3",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=60,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == ["SG-B3"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B3")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is not None
    assert reloaded.blocked_on_pr == 60


def test_release_stale_gated_tasks_variant_b_ignores_merged_task_without_pr_url(
    tmp_config_dir: Path,
) -> None:
    """A MERGED pr_state task with no pr_url (or an unparseable one) is
    excluded from the cross-reference index rather than raising."""
    blocking_no_url = _make_ticket_task(
        ticket_id="SG-B-BLOCKER3",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_state=PrState(state="MERGED"),
    )
    blocking_bad_url = _make_ticket_task(
        ticket_id="SG-B-BLOCKER4",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        pr_url="not-a-github-pr-url",
        pr_state=PrState(state="MERGED"),
    )
    blocked = _make_ticket_task(
        ticket_id="SG-B4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=61,
    )
    save_dev_queue(DevQueueStore(tasks=[blocking_no_url, blocking_bad_url, blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B4")
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_variant_b_no_cross_reference_is_noop(
    tmp_config_dir: Path,
) -> None:
    """Variant B blocking PR absent from the queue entirely (documents the
    residual cross-reference-only blind spot): no mutation, no false-positive
    stamp."""
    blocked = _make_ticket_task(
        ticket_id="SG-B2",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_gate_blocked",
        blocked_reason="prior_pipeline_pr_open",
        blocked_on_pr=999,
    )
    save_dev_queue(DevQueueStore(tasks=[blocked]))
    save_state(CwState(sessions=[]))

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-B2")
    assert reloaded.status == QueueItemStatus.BLOCKED_ON_USER
    assert reloaded.stale_gate_detected_at is None
    assert reloaded.blocked_on_pr == 999


def test_release_stale_gated_tasks_idempotent_cursor(
    tmp_config_dir: Path,
) -> None:
    """Two consecutive calls with no new events between them: the second call
    is a no-op (mirrors retire_merged_prs's idempotency shape)."""
    task = _make_ticket_task(
        ticket_id="SG-A4",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/45",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A4", repo="foo/bar", pr_number=45)

    first = release_stale_gated_tasks()
    second = release_stale_gated_tasks()

    assert first == ["SG-A4"]
    assert second == []


def test_release_stale_gated_tasks_ignores_non_gate_blocked_on_user(
    tmp_config_dir: Path,
) -> None:
    """A BLOCKED_ON_USER row with an unrelated disposition (signoff_gate)
    whose PR merges must not over-fire on every pr.merged event."""
    task = _make_ticket_task(
        ticket_id="SG-A5",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="signoff_gate",
        pr_url="https://github.com/foo/bar/pull/46",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A5", repo="foo/bar", pr_number=46)

    released = release_stale_gated_tasks()

    assert released == []
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A5")
    assert reloaded.stale_gate_detected_at is None


def test_release_stale_gated_tasks_lane_reap_policy_override(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane-level reap_policy=auto overrides a global signal_only default,
    mirroring park_terminal_sibling_tasks's existing precedence tests."""
    task = _make_ticket_task(
        ticket_id="SG-A6",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/47",
        lane="special",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    auto_lane_client = _client_with_lane(
        "client-a", "special", reap_policy=ReapPolicy.AUTO
    )
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_clients", lambda: {"client-a": auto_lane_client}
    )
    _emit_pr_merged(client="client-a", ticket_id="SG-A6", repo="foo/bar", pr_number=47)

    released = release_stale_gated_tasks()

    assert released == ["SG-A6"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A6")
    assert reloaded.status == QueueItemStatus.COMPLETED
    assert reloaded.disposition == "shipped"


def test_release_stale_gated_tasks_reap_proposed_carries_real_session_id(
    tmp_config_dir: Path,
) -> None:
    """SESSION_REAP_PROPOSED must carry the task's real session_id, not a
    hardcoded None (GitHub #1713 review SHOULD_FIX 5). The concrete
    downstream consumer, cli/orchestrate.py's _drain_reap_proposals, does
    ``payload.get("session_id", "")`` and looks that up against
    state.sessions -- a hardcoded None means the key IS present with value
    None, so ``.get`` returns None (not the "" default) and the lookup
    always misses, silently discarding which session was live when the
    gate was detected stale. Mirrors park_terminal_sibling_tasks's
    orig_session_id precedent: captured BEFORE the release helper clears
    task.session_id."""
    task = _make_ticket_task(
        ticket_id="SG-A8",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/49",
        session_id="live-session-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A8", repo="foo/bar", pr_number=49)

    released = release_stale_gated_tasks()

    assert released == ["SG-A8"]
    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "SG-A8"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["session_id"] == "live-session-1"


def test_release_stale_gated_tasks_event_not_lost_when_save_dev_queue_raises(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR_MERGED event must not be lost if save_dev_queue raises mid-call
    (GitHub #1713 review MUST_FIX 1: cursor-advances-before-persist bug).

    Pre-fix, ``advance_cursor`` ran unconditionally inside the Variant A
    loop, before the batched ``save_dev_queue`` that actually persists the
    release. A crash (or a raising ``save_dev_queue`` -- disk full, lock
    I/O) in that window permanently consumed the ``PR_MERGED`` event (fires
    once per PR, first-observation dedup) with no corresponding mutation
    ever written, leaving the task stuck BLOCKED_ON_USER forever. This test
    simulates that failure and confirms a retry, once ``save_dev_queue``
    works again, still sees (and releases) the task -- proving the cursor
    was not advanced on the failing call.
    """
    task = _make_ticket_task(
        ticket_id="SG-A7",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="merge_pending",
        pr_url="https://github.com/foo/bar/pull/48",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    save_state(CwState(sessions=[]))
    _emit_pr_merged(client="client-a", ticket_id="SG-A7", repo="foo/bar", pr_number=48)

    real_save_dev_queue = save_dev_queue

    def _raise_once(store: DevQueueStore) -> None:
        msg = "simulated save_dev_queue failure"
        raise OSError(msg)

    monkeypatch.setattr("cw.reconcile.tasks.save_dev_queue", _raise_once)

    with pytest.raises(OSError, match="simulated save_dev_queue failure"):
        release_stale_gated_tasks()

    # The mutation never persisted -- task is unchanged on disk.
    still_blocked = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A7")
    assert still_blocked.status == QueueItemStatus.BLOCKED_ON_USER
    assert still_blocked.stale_gate_detected_at is None

    # save_dev_queue works normally now. If the cursor had been advanced on
    # the failing call, this second pass would silently drop the event and
    # return []. It must instead release the task, proving the event was
    # not lost.
    monkeypatch.setattr("cw.reconcile.tasks.save_dev_queue", real_save_dev_queue)
    released = release_stale_gated_tasks()

    assert released == ["SG-A7"]
    reloaded = next(t for t in load_dev_queue().tasks if t.ticket_id == "SG-A7")
    assert reloaded.stale_gate_detected_at is not None
