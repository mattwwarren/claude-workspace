"""Unit tests for cw.reconcile.tasks.

Dev-queue revert backstops and timed-out-merged completion.
"""

from __future__ import annotations

from collections.abc import Callable
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
    PendingFixDispatch,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
    UsageLimitAct,
)
from cw.reconcile import (
    _DIRTY_WORKTREE_REASON,
    reconcile,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.worktree import OnBranch
from tests._dirty_check_helpers import (
    revert_completed_silent_prefetched,
    revert_timed_out_prefetched,
)
from tests._reconcile_helpers import (
    _make_pending_fix_dispatch,
    _mk_daemon_completed_session,
    _mk_daemon_session_with_worktree,
    _mk_session,
    _mk_timed_out_daemon_session,
)

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


@pytest.mark.parametrize(
    ("make_session", "backstop"),
    [
        pytest.param(
            _mk_daemon_completed_session,
            revert_completed_silent_tasks,
            id="completed-backstop",
        ),
        pytest.param(
            lambda sid: _mk_timed_out_daemon_session(sid, "TKT-ULA", datetime.now(UTC)),
            revert_timed_out_tasks,
            id="timed-out-backstop",
        ),
    ],
)
def test_backstops_skip_row_carrying_a_usage_limit_act(
    tmp_config_dir: Path,
    make_session: Callable[[str], Session],
    backstop: Callable[[], list[str]],
) -> None:
    """A row still carrying a mid-turn usage-limit act is never reverted (#2324).

    The act closed the session and resumes to finish the row uncharged; a
    backstop revert here would charge the attempt the act exists to spare,
    and stamp a reap reason for a reap that never happened.
    """
    sess = make_session("act-sess-1")
    save_state(CwState(sessions=[sess]))
    now = datetime.now(UTC)
    task = TicketTask(
        ticket_id="TKT-ULA",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-sess-1",
        usage_limit_act=UsageLimitAct(
            session_id="act-sess-1",
            branch="auto",
            started_at=now,
            reset_at=None,
            until=now + timedelta(minutes=30),
            audited_at=now,
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with sessions_lock():
        assert backstop() == []

    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.RUNNING
    assert stored.unproductive_attempts == 0
    assert stored.usage_limit_act is not None
    assert load_state().sessions[0].reap_reason is None


@pytest.mark.parametrize(
    ("make_session", "backstop"),
    [
        pytest.param(
            _mk_daemon_completed_session,
            revert_completed_silent_tasks,
            id="completed-backstop",
        ),
        pytest.param(
            lambda sid: _mk_timed_out_daemon_session(sid, "TKT-FDH", datetime.now(UTC)),
            revert_timed_out_tasks,
            id="timed-out-backstop",
        ),
    ],
)
def test_backstops_skip_row_carrying_pending_fix_dispatch(
    tmp_config_dir: Path,
    make_session: Callable[[str], Session],
    backstop: Callable[[], list[str]],
) -> None:
    """A row still carrying an unconsumed fix-loop handoff is never reverted (#2204).

    The row belongs to cw.reconcile.fix_dispatch for the whole handoff; a
    generic backstop revert here would strand pending_fix_dispatch on a
    PENDING row, which _build_dispatch_jobs then classifies as a stale
    handoff and drops instead of dispatching the fix session.
    """
    sess = make_session("fdh-sess-1")
    save_state(CwState(sessions=[sess]))
    pending: PendingFixDispatch = _make_pending_fix_dispatch()
    task = TicketTask(
        ticket_id="TKT-FDH",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="fdh-sess-1",
        pending_fix_dispatch=pending,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with sessions_lock():
        assert backstop() == []

    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.RUNNING
    assert stored.session_id == "fdh-sess-1"
    assert stored.unproductive_attempts == 0
    assert stored.pending_fix_dispatch is not None
    assert load_state().sessions[0].reap_reason is None


@pytest.mark.parametrize(
    ("make_session", "backstop"),
    [
        pytest.param(
            _mk_daemon_completed_session,
            revert_completed_silent_tasks,
            id="completed-backstop",
        ),
        pytest.param(
            lambda sid: _mk_timed_out_daemon_session(
                sid, "TKT-FDSID", datetime.now(UTC)
            ),
            revert_timed_out_tasks,
            id="timed-out-backstop",
        ),
    ],
)
def test_backstops_skip_row_carrying_fix_dispatch_session_id(
    tmp_config_dir: Path,
    make_session: Callable[[str], Session],
    backstop: Callable[[], list[str]],
) -> None:
    """A row with a live fix_dispatch_session_id is never reverted (#2204).

    Same exemption as pending_fix_dispatch, for the other half of the
    handoff's lifetime: once the fix session has spawned, the row still
    belongs to fix_dispatch until that session completes.
    """
    sess = make_session("fdsid-sess-1")
    save_state(CwState(sessions=[sess]))
    task = TicketTask(
        ticket_id="TKT-FDSID",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="fdsid-sess-1",
        fix_dispatch_session_id="fix-sess-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with sessions_lock():
        assert backstop() == []

    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.RUNNING
    assert stored.session_id == "fdsid-sess-1"
    assert stored.unproductive_attempts == 0
    assert stored.fix_dispatch_session_id == "fix-sess-1"
    assert load_state().sessions[0].reap_reason is None


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
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/to-dirty"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: "2 uncommitted path(s)",
    )

    reverted = revert_timed_out_prefetched()

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
    assert p["breadcrumbs"] == f"{wt_path}: 2 uncommitted path(s)"


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
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/to-clean"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: None,
    )

    reverted = revert_timed_out_prefetched()

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
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/to-clean-marker"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: None,
    )

    reverted = revert_timed_out_prefetched()

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
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/cs-dirty"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: "2 uncommitted path(s)",
    )

    reverted = revert_completed_silent_prefetched()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "cs-dirty")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None
    assert "cs-dirty" not in reverted

    events = read_events(
        consumer="test-cs-dirty-attn",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["breadcrumbs"] == f"{wt_path}: 2 uncommitted path(s)"


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
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/cs-clean"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: None,
    )

    reverted = revert_completed_silent_prefetched()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "cs-clean")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert "cs-clean" in reverted


# ---------------------------------------------------------------------------
# Completion-routing grace window (#2355)
# ---------------------------------------------------------------------------


def test_revert_completed_silent_tasks_within_grace_window_skips_dirty_check(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED session inside its post-completion grace window is left
    RUNNING entirely -- the dirty check never even runs (#2355)."""
    wt_path = tmp_path / "wt-grace-dirty"
    completed_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_daemon_session_with_worktree(
        "grace-dirty", SessionStatus.COMPLETED, wt_path
    ).model_copy(update={"completed_at": completed_at})
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-dirty",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-dirty",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-dirty"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    call_count = 0

    def _record_call(_c: str, _b: str, **_kw: object) -> str:
        nonlocal call_count
        call_count += 1
        return "2 uncommitted path(s)"

    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason", _record_call
    )

    with freezegun.freeze_time(completed_at + timedelta(seconds=10)):
        reverted = revert_completed_silent_tasks()

    assert reverted == []
    assert call_count == 0

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "grace-dirty")
    assert updated_task.status == QueueItemStatus.RUNNING
    assert updated_task.session_id == "grace-dirty"

    events = read_events(
        consumer="test-grace-dirty-attn",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert events == []


def test_revert_completed_silent_tasks_within_grace_window_skips_clean_revert(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED session inside its grace window is left RUNNING even when
    its worktree is clean -- the plain-revert-to-PENDING path is gated too,
    not just the dirty-park path (#2355)."""
    wt_path = tmp_path / "wt-grace-clean"
    completed_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_daemon_session_with_worktree(
        "grace-clean", SessionStatus.COMPLETED, wt_path
    ).model_copy(update={"completed_at": completed_at})
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-clean",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-clean",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-clean"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: None,
    )

    with freezegun.freeze_time(completed_at + timedelta(seconds=10)):
        reverted = revert_completed_silent_tasks()

    assert reverted == []

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "grace-clean")
    assert updated_task.status == QueueItemStatus.RUNNING
    assert updated_task.session_id == "grace-clean"


def test_revert_completed_silent_tasks_past_grace_window_dirty_still_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the grace window, a dirty COMPLETED session still parks to
    BLOCKED_ON_USER/dirty_worktree -- today's safety net, unweakened, only
    delayed (#2355 regression guard)."""
    wt_path = tmp_path / "wt-grace-dirty-past"
    completed_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_daemon_session_with_worktree(
        "grace-dirty-past", SessionStatus.COMPLETED, wt_path
    ).model_copy(update={"completed_at": completed_at})
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-dirty-past",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-dirty-past",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-dirty-past"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: "2 uncommitted path(s)",
    )

    with freezegun.freeze_time(completed_at + timedelta(seconds=61)):
        reverted = revert_completed_silent_prefetched()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "grace-dirty-past")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None
    assert "grace-dirty-past" not in reverted

    events = read_events(
        consumer="test-grace-dirty-past-attn",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].payload["paused_status"] == _DIRTY_WORKTREE_REASON


def test_revert_completed_silent_tasks_past_grace_window_clean_still_reverts(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the grace window, a clean COMPLETED session still reverts to
    PENDING -- no regression on the clean-past-grace path (#2355)."""
    wt_path = tmp_path / "wt-grace-clean-past"
    completed_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_daemon_session_with_worktree(
        "grace-clean-past", SessionStatus.COMPLETED, wt_path
    ).model_copy(update={"completed_at": completed_at})
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-clean-past",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-clean-past",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-clean-past"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: None,
    )

    with freezegun.freeze_time(completed_at + timedelta(seconds=61)):
        reverted = revert_completed_silent_prefetched()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "grace-clean-past")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None
    assert "grace-clean-past" in reverted


def test_revert_completed_silent_tasks_missing_completed_at_gets_no_grace(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMPLETED session with completed_at=None gets no grace at all --
    fail-safe direction: never silently grant indefinite protection (#2355)."""
    wt_path = tmp_path / "wt-grace-no-completed-at"
    sess = _mk_daemon_session_with_worktree(
        "grace-no-completed-at", SessionStatus.COMPLETED, wt_path
    )
    assert sess.completed_at is None
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-no-completed-at",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-no-completed-at",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-no-completed-at"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason",
        lambda _c, _b, **_kw: "2 uncommitted path(s)",
    )

    reverted = revert_completed_silent_prefetched()

    store = load_dev_queue()
    updated_task = next(
        t for t in store.tasks if t.ticket_id == "grace-no-completed-at"
    )
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None
    assert "grace-no-completed-at" not in reverted


def test_revert_timed_out_tasks_within_grace_window_skips_dirty_check(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT session inside its grace window is left RUNNING entirely --
    the two revert wrappers must not silently diverge (#2355)."""
    wt_path = tmp_path / "wt-grace-to-dirty"
    completed_at = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_daemon_session_with_worktree(
        "grace-to-dirty", SessionStatus.TIMED_OUT, wt_path
    ).model_copy(update={"completed_at": completed_at})
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="grace-to-dirty",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="grace-to-dirty",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.probe_checked_out_branch",
        lambda _p: OnBranch("auto-dev/grace-to-dirty"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    call_count = 0

    def _record_call(_c: str, _b: str, **_kw: object) -> str:
        nonlocal call_count
        call_count += 1
        return "2 uncommitted path(s)"

    monkeypatch.setattr(
        "cw.reconcile._shared._worktree_evidence.unsaved_work_reason", _record_call
    )

    with freezegun.freeze_time(completed_at + timedelta(seconds=10)):
        reverted = revert_timed_out_tasks()

    assert reverted == []
    assert call_count == 0

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "grace-to-dirty")
    assert updated_task.status == QueueItemStatus.RUNNING
    assert updated_task.session_id == "grace-to-dirty"

    events = read_events(
        consumer="test-grace-to-dirty-attn",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert events == []


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
