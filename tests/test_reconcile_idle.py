"""Unit tests for cw.reconcile.idle.

Idle-watchdog (silently idle) sweep: detect/act candidates, salvage/park,
usage-limit vs idle-stall cause, classify-threshold routing, budget/retry-cap
resolution, and idle-advance sentinel backstop.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import freezegun
import pytest

from cw.auto_dev_result import (
    AutoDevResult,
)
from cw.config import (
    load_state,
    save_state,
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
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    _SILENTLY_IDLE_REASON,
    IDLE_WATCHDOG_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    UsageLimitDetection,
    flag_silently_idle_daemon_sessions,
    reconcile,
    resolve_idle_watchdog_budget,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_daemon_completed_session,
    _mk_headless_daemon_session,
    _mk_live_idle_daemon_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _state_queue_snapshot,
    _ul_record,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)
from tests.conftest import (
    _make_ticket_task,
    _write_idle_transcript,
)


def test_idle_routes_stage_complete_advance_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive-idle guard: ROUTE_EMITTED_SENTINEL already advances stage_complete.

    Companion to the phantom case (#716): when the surface is still alive in the
    roster and the emitted sentinel went unrouted past
    sentinel_unrouted_check_seconds, the idle path's ROUTE_EMITTED_SENTINEL must
    advance the stage (IMPL→REVIEW), not park or revert. This already worked;
    the test locks it so the phantom fix and the alive path stay consistent.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-stage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past sentinel_unrouted_check_seconds (300) but under the idle budget (900).
    now = started_at + timedelta(seconds=400)

    sess = _mk_headless_daemon_session("idle-stage", worktree, started_at)
    sess.last_result = None  # sentinel NOT yet consumed → ROUTE_EMITTED eligible
    payload = _stage_complete_payload()
    payload["ticket_id"] = "idle-stage"
    _write_salvage_transcript(home, worktree, "claude-idle-stage", payload)
    save_state(CwState(sessions=[sess]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-stage",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-stage",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-stage")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-stage")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING


def test_idle_stage_mismatch_does_not_orphan_task_or_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1031: a stale/replayed ROUTE_EMITTED_SENTINEL must not orphan
    the task or complete a daemon-live session (extends #1019's phantom-path
    guard to the alive-idle path).

    Same shape as ``test_idle_routes_stage_complete_advance_sentinel``, except
    the task's row has already advanced to REVIEW by the time the late/replayed
    stage_complete/stage2_impl sentinel is discovered (the #986 shape). The
    staged-advance guard must refuse: task stays exactly as it was, session is
    NOT completed, and the daemon surface is never stopped -- an unconditional
    completion here would tear down a surface the daemon still reports alive.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-mismatch"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=400)

    sess = _mk_headless_daemon_session("idle-mismatch", worktree, started_at)
    sess.last_result = None  # sentinel NOT yet consumed → ROUTE_EMITTED eligible
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "idle-mismatch"
    _write_salvage_transcript(home, worktree, "claude-idle-mismatch", payload)
    save_state(CwState(sessions=[sess]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-mismatch",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-mismatch",
                    # Row already advanced past IMPL by the time this stale
                    # IMPL-leg sentinel is discovered -- the #986 shape.
                    stage=Stage.REVIEW,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-mismatch")
    assert reloaded.status != SessionStatus.COMPLETED

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-mismatch")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.RUNNING
    assert task.disposition is None

    mock_daemon.stop.assert_not_called()


def test_idle_race_already_failed_task_does_not_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1189: a task raced to FAILED by a concurrent caller must not be
    completed by the alive-idle ROUTE_EMITTED_SENTINEL path.

    Same shape as ``test_idle_routes_stage_complete_advance_sentinel``, except
    the dev-queue task has already been landed FAILED/abandoned for this same
    ticket/session by the time the idle sweep's own routed-sentinel lookup
    runs (the R3(a) lookup-miss race). The lookup must report routed=False;
    the session must NOT be completed and the task must stay untouched.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-race"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=400)

    sess = _mk_headless_daemon_session("idle-race", worktree, started_at)
    sess.last_result = None  # sentinel NOT yet consumed → ROUTE_EMITTED eligible
    payload = _stage_complete_payload()
    payload["ticket_id"] = "idle-race"
    _write_salvage_transcript(home, worktree, "claude-idle-race", payload)
    save_state(CwState(sessions=[sess]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-race",
                    client="client-a",
                    # Already raced to terminal FAILED by a concurrent caller
                    # before this sweep's own lookup runs.
                    status=QueueItemStatus.FAILED,
                    session_id="idle-race",
                    stage=Stage.IMPL,
                    disposition="abandoned",
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-race")
    assert reloaded.status != SessionStatus.COMPLETED

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-race")
    assert task.status == QueueItemStatus.FAILED
    assert task.disposition == "abandoned"


def test_idle_own_call_blocked_result_failed_landing_does_not_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1189 bonus: idle.py's own routed-sentinel call can land the
    task terminal-FAILED directly via ``_route_blocked_result_to_task``.

    Unlike phantom.py/stalled.py, idle.py's ROUTE_EMITTED_SENTINEL candidate
    build has no ``isinstance(result, AutoDevResult)`` filter, so a malformed/
    BlockedResult sentinel in the transcript is routed here too. Before the
    fix, ``_route_blocked_result_to_task``'s ``None`` return meant `routed`
    stayed True even though this same call just landed the task FAILED --
    completing a session whose task independently failed. Assert the session
    is NOT completed.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-blockedresult"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=400)

    sess = _mk_headless_daemon_session("idle-blockedres", worktree, started_at)
    sess.last_result = None  # sentinel NOT yet consumed → ROUTE_EMITTED eligible
    # A malformed sentinel (unrecognised status) parses as a BlockedResult,
    # not an AutoDevResult -- idle.py has no isinstance filter to exclude it.
    _write_salvage_transcript(
        home,
        worktree,
        "claude-idle-blockedres",
        {"schema_version": 4, "ticket_id": "idle-blockedres", "status": "proceed"},
    )
    save_state(CwState(sessions=[sess]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-blockedres",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-blockedres",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-blockedres")
    assert reloaded.status != SessionStatus.COMPLETED

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-blockedres")
    assert task.status == QueueItemStatus.FAILED
    assert task.disposition == "abandoned"


def test_flag_silently_idle_daemon_sessions_transitions_past_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """DAEMON ACTIVE + no last_result + started >IDLE_WATCHDOG + cap exhausted →
    BLOCKED_ON_USER park (#348, updated for #384: park only when attempts >= cap)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="silent-1",
        name="client-a/auto-dev/SILENT-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="SILENT-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="silent-1",
        attempts=2,  # at DEFAULT_IDLE_RETRY_CAP → park path (#384)
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "SILENT-1" in blocked
    # #348: flag-only — session stays ACTIVE so daemon worker can keep running.
    # Operators disposition flagged sessions via `cw spawn complete` /
    # `cw doctor --reap`. last_result.paused_status still set; the
    # _salvage_low_path idempotency guard (not _has_terminal_sentinel) prevents
    # double-firing on subsequent ticks (#324, #418).
    assert sess.status == SessionStatus.ACTIVE
    assert sess.completed_at is None
    assert sess.completed_reason is None
    assert sess.last_result == {"paused_status": _SILENTLY_IDLE_REASON}

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "SILENT-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer="test-silent-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["session_id"] == "silent-1"
    assert payload["paused_status"] == _SILENTLY_IDLE_REASON
    assert payload["crashed"] is False


def test_flag_silently_idle_watchdog_does_not_stop_working_worker(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """#348 intent preserved (#384): a worker still doing work is never stopped.

    Pre-#384 enforced by flag-only. Post-#384 enforced by the liveness gate:
    a worker the liveness check considers alive (recent write OR awaiting
    subagent) is skipped entirely, so stop() is never reached.
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="working-1",
        name="client-a/auto-dev/WORK-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="WORK-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="working-1",
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=True),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
    ):
        result, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=_auto_config()
        )

    mock_daemon.stop.assert_not_called()
    assert result == []
    assert sess.status == SessionStatus.ACTIVE
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.RUNNING


def test_flag_silently_idle_watchdog_no_double_fire_on_crash_recovery(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Crash recovery: session COMPLETED+last_result on disk, queue still RUNNING.

    Simulates the on-disk state after a crash between save_state (succeeded)
    and save_dev_queue (not yet called). The watchdog must skip the session
    because it is no longer in _LIVE_STATUSES, preventing a duplicate
    SESSION_NEEDS_ATTENTION event. (GitHub #324)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)

    sess = Session(
        id="crash-silent",
        name="client-a/auto-dev/CRASH-S",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        last_result={"paused_status": _SILENTLY_IDLE_REASON},
        completed_at=now,
        completed_reason=CompletionReason.NORMAL,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="CRASH-S",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="crash-silent",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    events = read_events(
        consumer="test-crash-recovery",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 0


def test_reconcile_converges_completed_daemon_session_running_task(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction-3 convergence: COMPLETED DAEMON session + RUNNING task → PENDING.

    Constructs the on-disk inconsistency that results from a crash between
    save_state (session → COMPLETED) and save_dev_queue (task → PENDING).
    Asserts that a single reconcile() tick converges the task to PENDING
    via revert_completed_silent_tasks(). See GitHub #867.
    """
    sess = _mk_daemon_completed_session("conv-sess-867")
    sess.name = "client-a/auto-dev/CONV-867"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="CONV-867",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="conv-sess-867",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    report = reconcile()

    assert "CONV-867" in report.reverted_ticket_ids
    store = load_dev_queue()
    reverted = next(t for t in store.tasks if t.ticket_id == "CONV-867")
    assert reverted.status == QueueItemStatus.PENDING
    assert reverted.session_id is None


def test_flag_silently_idle_daemon_sessions_leaves_under_budget_alone(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session started under the watchdog budget → not flagged."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() < IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="under-silent",
        name="client-a/auto-dev/UNDER-S",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_flag_silently_idle_daemon_sessions_skips_session_with_terminal_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with last_result (terminal sentinel already stored) → not touched."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="has-sentinel",
        name="client-a/auto-dev/HAS-S",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        last_result={"status": "shipped", "ticket_id": "HAS-S"},
    )
    state = CwState(sessions=[sess])
    save_state(state)

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_blocked_terminal_sentinel_suppresses_watchdog(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with last_result={"status": "blocked", ...} → treated as terminal,
    watchdog skips it. Regression guard: ensures fix does not gate on
    SALVAGE_TERMINAL_STATUSES (which excludes "blocked")."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="has-blocked-sentinel",
        name="client-a/auto-dev/HAS-BLK",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        last_result={
            "status": "blocked",
            "blocker": {"reason": "impl_failed"},
        },
    )
    state = CwState(sessions=[sess])
    save_state(state)

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_flag_silently_idle_daemon_sessions_skips_user_origin(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """USER-origin session → not touched by watchdog."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="user-silent",
        name="client-a/user-silent",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.USER,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# Idle-watchdog sentinel-salvage tests (GitHub issue #398): sessions that
# emitted a shipped/no_op sentinel but haven't had it consumed into
# last_result yet must be COMPLETED, not parked as BLOCKED_ON_USER.
# ---------------------------------------------------------------------------


def test_flag_silently_idle_salvages_shipped_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle-past-budget session with shipped sentinel in transcript → COMPLETED."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = _mk_headless_daemon_session("idle-salv-1", worktree, started_at)
    sess.last_result = None  # sentinel NOT yet consumed into state
    _write_salvage_transcript(
        home, worktree, "claude-idle-uuid-1", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    # B2: apply_staged_decision needs a pipeline to decide COMPLETED vs advance.
    # Ship at FINALIZE (terminal) → COMPLETED; must have clients.yaml on disk.
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-salv-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-salv-1",
                    attempts=2,  # at cap — would park without salvage
                    stage=Stage.FINALIZE,  # terminal; shipped here → COMPLETED
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        # Transcript mtime is real-time (May 2026) but now is fake (Jan 2026);
        # negative diff < window_seconds would falsely mark the worker alive.
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
    ):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-salv-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-salv-1")
    assert task.status == QueueItemStatus.COMPLETED

    mock_daemon.stop.assert_called_once_with("fake-short-id")


def test_flag_silently_idle_salvages_no_op_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle-past-budget session with no_op sentinel in transcript → COMPLETED."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-idle-noop"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = _mk_headless_daemon_session("idle-noop-1", worktree, started_at)
    sess.last_result = None
    _write_salvage_transcript(
        home, worktree, "claude-idle-uuid-2", _no_op_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-noop-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-noop-1",
                    attempts=2,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
    ):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "idle-noop-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "no_op"

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-noop-1")
    assert task.status == QueueItemStatus.COMPLETED

    mock_daemon.stop.assert_called_once_with("fake-short-id")


def test_silently_idle_parked_session_salvaged_on_next_pass(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#418: ACTIVE session parked silently_idle with shipped sentinel in transcript
    → salvaged on next watchdog pass, not at 60-min wall-clock timeout."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-418-park"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = _mk_headless_daemon_session("418-park-1", worktree, started_at)
    # Session was previously parked silently_idle — this is the #418 bug state.
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    _write_salvage_transcript(
        home, worktree, "claude-418-uuid-1", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="418-park-1",
                    client="client-a",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id="418-park-1",
                    attempts=2,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
    ):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=_auto_config()
        )

    assert blocked == []
    reloaded = next(s for s in load_state().sessions if s.id == "418-park-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"

    # Queue task stays BLOCKED_ON_USER — queue-task auto-complete for
    # salvaged-from-park is out of scope for #418 (separate follow-up).
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "418-park-1")
    assert task.status == QueueItemStatus.BLOCKED_ON_USER

    mock_daemon.stop.assert_called_once_with("fake-short-id")


def test_flag_silently_idle_no_salvage_without_sentinel_still_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Idle-past-budget with no sentinel and attempts >= cap → parks BLOCKED_ON_USER.

    Existing park behavior preserved — salvage path does not regress it.
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    # No worktree_path → _salvage_terminal_result returns None
    sess = Session(
        id="idle-nosentinel",
        name="client-a/auto-dev/IDLE-NS",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="IDLE-NS",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-nosentinel",
                    attempts=2,  # at cap → park path
                )
            ]
        )
    )

    with patch("cw.reconcile._deps.fire_push_notification"):
        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "IDLE-NS" in blocked
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "IDLE-NS")
    assert task.status == QueueItemStatus.BLOCKED_ON_USER


def test_reconcile_includes_silently_idle_in_report(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() calls watchdog and includes BLOCKED_ON_USER ticket in report
    when attempts >= cap (park path, #384)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    # The daemon returns this session as live (surface still registered).
    live_short_id = "abcd1234"
    full_uuid = f"{live_short_id}-1111-2222-3333-000000000000"

    sess = Session(
        id="rcl-silent",
        name="client-a/auto-dev/RCL-S",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=live_short_id,
        started_at=started_at,
        # Pre-prime to N-1 so the reconcile() call reaches the default threshold
        # (idle_confirm_observations=2) on its first observation. (#545)
        idle_observation_count=1,
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="RCL-S",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="rcl-silent",
        attempts=2,  # at cap → park path (#384)
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": full_uuid}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "RCL-S" in report.reverted_ticket_ids

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "RCL-S")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# confirm-before-reap counter tests (GitHub issue #545)
# ---------------------------------------------------------------------------


def test_flag_silently_idle_daemon_sessions_external_counterparty_escalates(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: derive_counterparty="external" -> SESSION_NEEDS_ATTENTION
    fires and the daemon session is NOT torn down. RFC 0011 B1 (#1158)."""
    from cw.reconcile._shared import _EXTERNAL_COUNTERPARTY_IDLE_REASON

    monkeypatch.setattr(
        "cw.reconcile.idle.derive_counterparty", lambda _task, **_kw: "external"
    )

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="idle-ext-e2e-1",
        name="client-a/auto-dev/idle-ext-e2e-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ext-e2e",
        started_at=started_at,
        idle_observation_count=0,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="idle-ext-e2e-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-ext-e2e-1",
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.fire_push_notification") as mock_notify,
    ):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ext-e2e"},
            config=_auto_config(idle_confirm_observations=1),
        )
        mock_daemon.stop.assert_not_called()
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "idle-ext-e2e-1" not in blocked
    assert sess.status == SessionStatus.ACTIVE

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "idle-ext-e2e-1")
    assert t.status == QueueItemStatus.RUNNING

    events = read_events(
        consumer="test-idle-ext-e2e-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].payload["paused_status"] == _EXTERNAL_COUNTERPARTY_IDLE_REASON


def test_flag_silently_idle_daemon_sessions_unchanged_no_merged_param(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-merged, non-FINALIZE git session -> SALVAGE_GIT preserved (#1054).

    flag_silently_idle_daemon_sessions does not thread merged_ticket_ids, so
    the classify default (empty frozenset) must leave behavior unchanged.
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-unchanged-salvage-git"
    worktree.mkdir(parents=True)

    sess = Session(
        id="unchanged-sgit",
        name="client-a/auto-dev/UNCHANGED-SGIT",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=1,  # one shy of threshold=2
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="UNCHANGED-SGIT",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="unchanged-sgit",
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/unchanged-sgit-branch",
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    blocked, salvage_git = flag_silently_idle_daemon_sessions(
        state,
        now=now,
        native_live={"live-ref"},
        config=_auto_config(idle_confirm_observations=2),
    )

    assert blocked == []
    assert len(salvage_git) == 1
    assert salvage_git[0][0] == "unchanged-sgit"
    assert salvage_git[0][2] == "auto-dev/unchanged-sgit-branch"


def test_resolve_idle_watchdog_budget_returns_global_default_with_no_task() -> None:
    """No task → global IDLE_WATCHDOG_SECONDS fallback."""
    config = _auto_config()
    assert resolve_idle_watchdog_budget(None, config) == IDLE_WATCHDOG_SECONDS


def test_resolve_idle_watchdog_budget_no_scope_hint_returns_global_default() -> None:
    """Task with no scope_hint → global fallback."""
    config = _auto_config()
    task = TicketTask(ticket_id="T-1", client="c", scope_hint=None)
    assert resolve_idle_watchdog_budget(task, config) == IDLE_WATCHDOG_SECONDS


def test_resolve_idle_watchdog_budget_respects_per_tier() -> None:
    """scope_hint='large' → idle_watchdog_by_tier['large'] returned."""
    config = _auto_config(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="large")
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_resolve_idle_watchdog_budget_global_config_override_no_task() -> None:
    """config.idle_watchdog_seconds overrides the hardcoded fallback (no task)."""
    config = _auto_config(idle_watchdog_seconds=1800)
    assert resolve_idle_watchdog_budget(None, config) == 1800


def test_resolve_idle_watchdog_budget_global_config_override_no_scope_hint() -> None:
    """A pre-Stage-1 task (no scope_hint) uses the global config override, not
    the hardcoded 900s — this is the fanout-cascade fix (workers reaped mid-work)."""
    config = _auto_config(idle_watchdog_seconds=1800)
    task = TicketTask(ticket_id="T-1", client="c", scope_hint=None)
    assert resolve_idle_watchdog_budget(task, config) == 1800


def test_resolve_idle_watchdog_budget_per_tier_beats_global_override() -> None:
    """A resolvable per-tier budget still wins over the global config default."""
    config = _auto_config(
        idle_watchdog_seconds=1800, idle_watchdog_by_tier={"large": 600}
    )
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="large")
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_resolve_idle_watchdog_budget_per_ticket_overrides_tier() -> None:
    """idle_watchdog_override beats per-tier dict."""
    config = _auto_config(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(
        ticket_id="T-1", client="c", scope_hint="large", idle_watchdog_override=900
    )
    assert resolve_idle_watchdog_budget(task, config) == 900


def test_flag_silently_idle_daemon_sessions_respects_large_tier_override(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Large-tier task at 1200s: > default 900s but < tier 1800s → not flagged."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)  # 1200 seconds elapsed
    elapsed = (now - started_at).total_seconds()
    assert elapsed > IDLE_WATCHDOG_SECONDS  # 1200 > 900 — would flag without override
    assert elapsed < 1800  # but under the large-tier override

    sess = Session(
        id="tier-silent",
        name="client-a/auto-dev/TIER-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="TIER-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="tier-silent",
        scope_hint="large",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    config = _auto_config(idle_watchdog_by_tier={"large": 1800})
    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=config
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_resolve_idle_watchdog_budget_unknown_tier_falls_back_to_global() -> None:
    """scope_hint not in idle_watchdog_by_tier → global IDLE_WATCHDOG_SECONDS."""
    config = _auto_config(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="unknown")
    assert resolve_idle_watchdog_budget(task, config) == IDLE_WATCHDOG_SECONDS


def test_resolve_idle_watchdog_budget_respects_per_stage() -> None:
    """stage=REVIEW with idle_watchdog_by_stage[REVIEW] set → that budget wins."""
    config = _auto_config(idle_watchdog_by_stage={Stage.REVIEW: 3600})
    task = TicketTask(ticket_id="T-1", client="c", stage=Stage.REVIEW)
    assert resolve_idle_watchdog_budget(task, config) == 3600


def test_resolve_idle_watchdog_budget_stage_beats_tier() -> None:
    """Per-stage budget fully overrides per-tier — no max() composition."""
    config = _auto_config(
        idle_watchdog_by_stage={Stage.REVIEW: 3600},
        idle_watchdog_by_tier={"large": 600},
    )
    task = TicketTask(
        ticket_id="T-1", client="c", stage=Stage.REVIEW, scope_hint="large"
    )
    assert resolve_idle_watchdog_budget(task, config) == 3600


def test_resolve_idle_watchdog_budget_override_beats_stage() -> None:
    """idle_watchdog_override still wins over the per-stage budget."""
    config = _auto_config(idle_watchdog_by_stage={Stage.REVIEW: 3600})
    task = TicketTask(
        ticket_id="T-1",
        client="c",
        stage=Stage.REVIEW,
        idle_watchdog_override=120,
    )
    assert resolve_idle_watchdog_budget(task, config) == 120


def test_resolve_idle_watchdog_budget_empty_stage_dict_falls_through_to_tier() -> None:
    """Default (empty) idle_watchdog_by_stage → falls through to per-tier."""
    config = _auto_config(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(ticket_id="T-1", client="c", stage=Stage.PLAN, scope_hint="large")
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_resolve_idle_watchdog_budget_absent_stage_falls_through_to_tier() -> None:
    """Populated idle_watchdog_by_stage but no entry for this task's stage
    → falls through to per-tier, same as an empty dict."""
    config = _auto_config(
        idle_watchdog_by_stage={Stage.REVIEW: 3600},
        idle_watchdog_by_tier={"large": 600},
    )
    task = TicketTask(
        ticket_id="T-1", client="c", stage=Stage.IMPL, scope_hint="large"
    )
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_flag_silently_idle_daemon_sessions_respects_review_stage_override(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """REVIEW-stage task at 1200s: > default 900s but < stage budget 3600s →
    not flagged."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)  # 1200 seconds elapsed
    elapsed = (now - started_at).total_seconds()
    assert elapsed > IDLE_WATCHDOG_SECONDS  # 1200 > 900 — would flag without override
    assert elapsed < 3600  # but under the review-stage override

    sess = Session(
        id="stage-silent",
        name="client-a/auto-dev/STAGE-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="STAGE-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="stage-silent",
        stage=Stage.REVIEW,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    config = _auto_config(idle_watchdog_by_stage={Stage.REVIEW: 3600})
    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=config
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_flag_silently_idle_daemon_sessions_respects_per_ticket_override(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """idle_watchdog_override on task beats both tier and global defaults."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)  # 1200s elapsed
    elapsed = (now - started_at).total_seconds()
    assert elapsed > IDLE_WATCHDOG_SECONDS  # 1200 > 900 default
    assert elapsed < 1500  # under the per-ticket override

    sess = Session(
        id="ticket-silent",
        name="client-a/auto-dev/TICK-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="TICK-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="ticket-silent",
        idle_watchdog_override=1500,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    config = _auto_config()
    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=config
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# flag_silently_idle — transcript liveness check (GitHub #340)
# ---------------------------------------------------------------------------


def test_flag_silently_idle_skips_worker_with_recent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elapsed > budget but transcript recently written → no fire (GitHub #340)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)  # 1200s > IDLE_WATCHDOG_SECONDS
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-live"
    sess = _mk_headless_daemon_session("live-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    state = CwState(sessions=[sess])
    save_state(state)

    transcript = _write_idle_transcript(home, worktree, filename="live-ref-sess.jsonl")
    # Stamp at half the liveness window — well within TRANSCRIPT_LIVENESS_WINDOW_SECONDS
    half_window = TRANSCRIPT_LIVENESS_WINDOW_SECONDS // 2
    recent_ts = (now - timedelta(seconds=half_window)).timestamp()
    os.utime(str(transcript), (recent_ts, recent_ts))

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.last_result is None  # watchdog did not fire


def test_flag_silently_idle_fires_when_project_dir_missing(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worktree_path set but project dir absent → proceeds to fire watchdog."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-no-proj"
    sess = _mk_headless_daemon_session("no-proj-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    # Do NOT create the .claude/projects/<encoded>/ directory.
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="no-proj-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="no-proj-1",
                    attempts=2,  # at cap → park path (#384)
                )
            ]
        )
    )
    state = CwState(sessions=[sess])
    save_state(state)

    with patch("cw.reconcile._deps.fire_push_notification"):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "no-proj-1" in blocked


def test_flag_silently_idle_fires_when_session_id_file_missing(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known claude_session_id but the specific .jsonl doesn't exist → fires."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-missing-file"
    sess = _mk_headless_daemon_session("missing-file-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    sess.claude_session_id = "missing-uuid"
    # Create project dir but NOT the expected .jsonl.
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    (home / ".claude" / "projects" / encoded).mkdir(parents=True, exist_ok=True)

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="missing-file-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="missing-file-1",
                    attempts=2,  # at cap → park path (#384)
                )
            ]
        )
    )
    state = CwState(sessions=[sess])
    save_state(state)

    with patch("cw.reconcile._deps.fire_push_notification"):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "missing-file-1" in blocked


def test_flag_silently_idle_fires_when_transcript_predates_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript mtime <= started_at → stale-transcript guard fires watchdog."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-predates"
    sess = _mk_headless_daemon_session("predates-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="predates-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="predates-1",
                    attempts=2,  # at cap → park path (#384)
                )
            ]
        )
    )
    state = CwState(sessions=[sess])
    save_state(state)

    transcript = _write_idle_transcript(home, worktree)
    # Stamp before started_at — stale-transcript guard should reject this file.
    before_start = (started_at - timedelta(seconds=60)).timestamp()
    os.utime(str(transcript), (before_start, before_start))

    with patch("cw.reconcile._deps.fire_push_notification"):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "predates-1" in blocked


def test_flag_silently_idle_fires_on_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elapsed > budget and transcript older than liveness window → fires."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)  # 1200s > budget
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-stale-tx"
    sess = _mk_headless_daemon_session("stale-tx-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="stale-tx-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="stale-tx-1",
                    attempts=2,  # at cap → park path (#384)
                )
            ]
        )
    )
    state = CwState(sessions=[sess])
    save_state(state)

    transcript = _write_idle_transcript(home, worktree)
    # Stamp beyond the liveness window — stale, watchdog should fire
    past_window = TRANSCRIPT_LIVENESS_WINDOW_SECONDS + 80
    stale_ts = (now - timedelta(seconds=past_window)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    with patch("cw.reconcile._deps.fire_push_notification"):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "stale-tx-1" in blocked
    assert sess.last_result == {"paused_status": _SILENTLY_IDLE_REASON}


def test_flag_silently_idle_fires_when_no_transcript_in_project_dir(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elapsed > budget, project dir exists but no .jsonl → fires (grace case)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-no-tx"
    sess = _mk_headless_daemon_session("no-tx-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    # Create project dir but write no .jsonl files
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    (home / ".claude" / "projects" / encoded).mkdir(parents=True, exist_ok=True)

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="no-tx-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="no-tx-1",
                    attempts=2,  # at cap → park path (#384)
                )
            ]
        )
    )
    state = CwState(sessions=[sess])
    save_state(state)

    with patch("cw.reconcile._deps.fire_push_notification"):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert "no-tx-1" in blocked
    assert sess.last_result == {"paused_status": _SILENTLY_IDLE_REASON}


def test_flag_silently_idle_skips_with_known_session_id_and_recent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known claude_session_id → specific file checked; recent write skips watchdog."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    worktree = tmp_path / "wt-known-id"
    sess = _mk_headless_daemon_session("known-id-1", worktree, started_at)
    sess.surface_ref = "live-ref"
    sess.claude_session_id = "known-uuid"
    state = CwState(sessions=[sess])
    save_state(state)

    transcript = _write_idle_transcript(home, worktree, filename="known-uuid.jsonl")
    recent_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(str(transcript), (recent_ts, recent_ts))

    blocked, _salvage = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=_auto_config()
    )

    assert blocked == []
    assert sess.last_result is None


# ---------------------------------------------------------------------------
# Helpers for _awaiting_subagent tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _awaiting_subagent tests (Task A1)
# ---------------------------------------------------------------------------


def test_flag_silently_idle_skips_worker_awaiting_subagent(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A worker past the idle budget but awaiting a subagent is NOT flagged (#384)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = Session(
        id="busy-1",
        name="client-a/auto-dev/BUSY-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="BUSY-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="busy-1",
                )
            ]
        )
    )

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=True),
        patch("cw.reconcile._deps.fire_push_notification") as mock_notify,
    ):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=_auto_config()
        )
        mock_notify.assert_not_called()

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.RUNNING


# ---------------------------------------------------------------------------
# Task B1: resolve_idle_retry_cap + idle_retry_cap_by_tier config field
# ---------------------------------------------------------------------------


def test_resolve_idle_retry_cap_default_with_no_task() -> None:
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, resolve_idle_retry_cap

    assert resolve_idle_retry_cap(None, _auto_config()) == DEFAULT_IDLE_RETRY_CAP


def test_resolve_idle_retry_cap_respects_per_tier() -> None:
    from cw.reconcile import resolve_idle_retry_cap

    cfg = _auto_config(idle_retry_cap_by_tier={"large": 4})
    task = TicketTask(ticket_id="T", client="c", scope_hint="large")
    assert resolve_idle_retry_cap(task, cfg) == 4


def test_resolve_idle_retry_cap_unknown_tier_falls_back() -> None:
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, resolve_idle_retry_cap

    cfg = _auto_config(idle_retry_cap_by_tier={"large": 4})
    task = TicketTask(ticket_id="T", client="c", scope_hint="small")
    assert resolve_idle_retry_cap(task, cfg) == DEFAULT_IDLE_RETRY_CAP


# ---------------------------------------------------------------------------
# Task B2: auto-recover under cap, park on exhaustion
# ---------------------------------------------------------------------------


def test_flag_silently_idle_auto_recovers_under_cap(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Confirmed-idle worker, attempts < cap → surface stopped, task PENDING (#384)."""

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="hang-1",
        name="client-a/auto-dev/HANG-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="HANG-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="hang-1",
                    attempts=1,  # < DEFAULT_IDLE_RETRY_CAP (2)
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.fire_push_notification") as mock_notify,
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )
        mock_daemon.stop.assert_called_once_with("live-ref")
        mock_notify.assert_not_called()

    store = load_dev_queue()
    t = store.tasks[0]
    assert t.status == QueueItemStatus.PENDING
    assert t.session_id is None
    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.completed_reason == CompletionReason.TIMED_OUT
    events = read_events(
        consumer="test-hang-recover",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    assert events[0].payload["cause"] == "idle_stall_recovered"


def test_flag_silently_idle_recover_cleans_up_worktree(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Idle-stall recover (the second cleanup call site) removes the stale
    worktree so the re-dispatched ticket starts clean (#404)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="hang-wt",
        name="client-a/auto-dev/HANG-WT",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        branch="auto-dev/HANG-WT",
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="HANG-WT",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="hang-wt",
                    attempts=1,  # < cap → recover path
                )
            ]
        )
    )

    removed: list[tuple[str, str, bool]] = []
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile._deps.fire_push_notification"),
        patch(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        ),
        patch(
            "cw.reconcile._shared.remove_worktree",
            lambda client, branch, *, force=False: removed.append(
                (client.name, branch, force)
            ),
        ),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert removed == [("client-a", "auto-dev/HANG-WT", True)]
    assert sess.status == SessionStatus.TIMED_OUT


def test_flag_silently_idle_recover_skips_cleanup_when_no_branch(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Recover with no branch on the session attempts no worktree cleanup (#404)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="hang-nb",
        name="client-a/auto-dev/HANG-NB",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        # branch left as the model default (None)
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="HANG-NB",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="hang-nb",
                    attempts=1,
                )
            ]
        )
    )

    calls: list[str] = []

    def record_get_client(name: str) -> ClientConfig:
        calls.append(name)
        return ClientConfig(name=name, workspace_path=tmp_path / "ws")

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile._deps.fire_push_notification"),
        patch("cw.reconcile._shared.get_client", record_get_client),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    assert calls == []
    assert sess.status == SessionStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# GitHub #486: usage_limit_cutoff cause distinction
# ---------------------------------------------------------------------------


def test_flag_silently_idle_usage_limit_emits_distinct_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage-limit transcript → cause=usage_limit_cutoff on recover (#486)."""
    from cw.reconcile import _CAUSE_USAGE_LIMIT

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    worktree = tmp_path / "wt-ul"
    sess = _mk_headless_daemon_session("ul-1", worktree, started_at)
    sess.surface_ref = "ul-ref"
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="ul-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="ul-1",
                    attempts=1,  # < cap → recover path
                )
            ]
        )
    )

    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "You've hit your session limit · resets 5:20pm",
        filename="ul-ref-sess-486.jsonl",
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"ul-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    events = read_events(
        consumer="test-ul-cause",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    assert events[0].payload["cause"] == _CAUSE_USAGE_LIMIT


def test_flag_silently_idle_no_usage_limit_emits_idle_stall_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usage-limit text → cause=idle_stall_recovered on recover (regress, #486)."""
    from cw.reconcile import _CAUSE_IDLE_STALL

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    worktree = tmp_path / "wt-nostall"
    sess = _mk_headless_daemon_session("nostall-1", worktree, started_at)
    sess.surface_ref = "nostall-ref"
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="nostall-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="nostall-1",
                    attempts=1,  # < cap → recover path
                )
            ]
        )
    )

    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "Working on the implementation now.",
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"nostall-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    events = read_events(
        consumer="test-nostall-cause",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    assert events[0].payload["cause"] == _CAUSE_IDLE_STALL


def test_flag_silently_idle_parks_when_cap_exhausted(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Confirmed-idle worker, attempts >= cap → BLOCKED_ON_USER park (#384)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="hang-2",
        name="client-a/auto-dev/HANG-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="HANG-2",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="hang-2",
                    attempts=2,  # == DEFAULT_IDLE_RETRY_CAP
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.fire_push_notification") as mock_notify,
    ):
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )
        mock_daemon.stop.assert_not_called()
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "HANG-2" in blocked
    assert sess.status == SessionStatus.ACTIVE
    assert sess.last_result == {"paused_status": "silently_idle"}
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
    assert store.tasks[0].disposition == _SILENTLY_IDLE_REASON


def test_classify_idle_threshold_finalize_stage_returns_none(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FINALIZE-stage worktree+branch session -> None; defer to stalled.py (#1054)."""
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt_path = tmp_path / "wt-finalize-classify"
    wt_path.mkdir(parents=True)
    sess = Session(
        id="idle-finalize-1",
        name="client-a/auto-dev/idle-finalize-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=wt_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-finalize-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-finalize-1",
        stage=Stage.FINALIZE,
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-finalize-1"
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-finalize-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        merged_client_ticket_ids=frozenset(),
    )

    assert candidate is None


def test_classify_idle_threshold_merged_routes_to_revert_task(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged ticket, worktree+branch, any stage -> REVERT_TASK not SALVAGE_GIT.

    See GitHub #1054.
    """
    from cw.reconcile import ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt_path = tmp_path / "wt-merged-classify"
    wt_path.mkdir(parents=True)
    sess = Session(
        id="idle-merged-classify-1",
        name="client-a/auto-dev/idle-merged-classify-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=wt_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-merged-classify-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-merged-classify-1",
        stage=Stage.IMPL,
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/idle-merged-classify-1",
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-merged-classify-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        merged_client_ticket_ids=frozenset({("client-a", "idle-merged-classify-1")}),
    )

    assert candidate is not None
    assert candidate.proposed_action == ProposedAction.REVERT_TASK


def test_classify_idle_threshold_merged_different_client_not_routed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged ticket for a DIFFERENT client must not route this session's
    same-numbered ticket to REVERT_TASK (cross-client collision guard, #1054).

    ticket_id strings are not globally unique across clients (dev_queue.py
    keys ticket lookups on (ticket_id, client)); merged_client_ticket_ids must
    be consulted as a (client, ticket_id) pair, not a bare ticket_id.
    """
    from cw.reconcile import ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt_path = tmp_path / "wt-collision-classify"
    wt_path.mkdir(parents=True)
    sess = Session(
        id="idle-collision-1",
        name="client-b/auto-dev/COLLIDE-1",
        client="client-b",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=wt_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="COLLIDE-1",
        client="client-b",
        status=QueueItemStatus.RUNNING,
        session_id="idle-collision-1",
        stage=Stage.IMPL,
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/collide-1",
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    # client-a's "COLLIDE-1" is merged; client-b's session with the same
    # ticket_id string must NOT be treated as merged.
    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="COLLIDE-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        merged_client_ticket_ids=frozenset({("client-a", "COLLIDE-1")}),
    )

    assert candidate is not None
    assert candidate.proposed_action == ProposedAction.SALVAGE_GIT


def test_classify_idle_threshold_non_finalize_worktree_still_salvage_git(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-FINALIZE worktree+branch, not merged -> still SALVAGE_GIT.

    Regression check; see GitHub #1054.
    """
    from cw.reconcile import ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt_path = tmp_path / "wt-nonfinalize-classify"
    wt_path.mkdir(parents=True)
    sess = Session(
        id="idle-nonfinalize-1",
        name="client-a/auto-dev/idle-nonfinalize-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=wt_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-nonfinalize-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-nonfinalize-1",
        stage=Stage.IMPL,
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/idle-nonfinalize-1",
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-nonfinalize-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        merged_client_ticket_ids=frozenset(),
    )

    assert candidate is not None
    assert candidate.proposed_action == ProposedAction.SALVAGE_GIT


def test_classify_idle_threshold_external_counterparty_escalates_instead_of_parking(
    tmp_config_dir: Path,
) -> None:
    """counterparty="external" -> ESCALATE_EXTERNAL_IDLE, not PARK_BLOCKED_ON_USER.

    RFC 0011 B1 (#1158): a session reviewing a teammate's PR is exempt from
    silent idle-reap and is escalated instead.
    """
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, ProposedAction
    from cw.reconcile._shared import _EXTERNAL_COUNTERPARTY_IDLE_REASON
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = Session(
        id="idle-ext-1",
        name="client-a/auto-dev/idle-ext-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=None,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-ext-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-ext-1",
        attempts=DEFAULT_IDLE_RETRY_CAP,
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-ext-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        counterparty="external",
    )

    assert candidate is not None
    assert candidate.proposed_action == ProposedAction.ESCALATE_EXTERNAL_IDLE
    assert candidate.paused_status == _EXTERNAL_COUNTERPARTY_IDLE_REASON


def test_classify_idle_threshold_external_counterparty_merged_still_completes(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """counterparty="external" + merged ticket -> merged-first check still wins.

    A shipped ticket has nothing left to escalate; REVERT_TASK must be
    returned regardless of the counterparty axis. RFC 0011 B1 (#1158).
    """
    from cw.reconcile import ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt_path = tmp_path / "wt-ext-merged-classify"
    wt_path.mkdir(parents=True)
    sess = Session(
        id="idle-ext-merged-1",
        name="client-a/auto-dev/idle-ext-merged-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=wt_path,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-ext-merged-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-ext-merged-1",
        stage=Stage.IMPL,
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/idle-ext-merged-1",
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-ext-merged-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
        merged_client_ticket_ids=frozenset({("client-a", "idle-ext-merged-1")}),
        counterparty="external",
    )

    assert candidate is not None
    assert candidate.proposed_action == ProposedAction.REVERT_TASK


def test_classify_idle_threshold_self_counterparty_unchanged(
    tmp_config_dir: Path,
) -> None:
    """counterparty defaulted (="self") -> PARK_BLOCKED_ON_USER unchanged.

    Regression guard for RFC 0011 B1 (#1158): existing callers that don't
    yet pass counterparty must see identical behavior to before this ticket.
    """
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = Session(
        id="idle-self-1",
        name="client-a/auto-dev/idle-self-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=None,
        surface_ref="live-ref",
        started_at=started_at,
    )
    task = TicketTask(
        ticket_id="idle-self-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-self-1",
        attempts=DEFAULT_IDLE_RETRY_CAP,
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-self-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
    )

    assert candidate.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    assert candidate.paused_status == _SILENTLY_IDLE_REASON


# ---------------------------------------------------------------------------
# _awaiting_subagent edge-case coverage
# ---------------------------------------------------------------------------


def test_flag_silently_idle_recover_skips_non_running_task(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Queue task not RUNNING is not mutated during recover/park sweep (#384)."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = Session(
        id="skip-nonrunning",
        name="client-a/auto-dev/SKIP-NR",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    # Task is PENDING (not RUNNING) — should be left alone
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="SKIP-NR",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                    attempts=1,  # < cap → would recover if RUNNING
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._deps.checked_out_branch", return_value=None),
    ):
        result, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )
        # Surface still stopped (recover path, attempts=1 < cap=2)
        mock_daemon.stop.assert_called_once_with("live-ref")

    # Task was PENDING, not RUNNING → not touched
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.PENDING
    assert result == []


# ---------------------------------------------------------------------------
# GitHub issue #432: malformed roster JSON + FileNotFoundError must not
# crash reconcile; idle_watchdog_seconds=0 must be honored (not 900 fallback).
# ---------------------------------------------------------------------------


def test_resolve_idle_watchdog_honors_zero(
    tmp_config_dir: Path,
) -> None:
    """idle_watchdog_seconds=0 is honored as 0, not silently replaced by 900.

    The `or` operator treats 0 as falsy and falls back to the constant.
    The fix uses an explicit None check so 0 passes through (#432).
    """
    config = _auto_config(idle_watchdog_seconds=0)
    budget = resolve_idle_watchdog_budget(task=None, config=config)
    assert budget == 0, (
        f"idle_watchdog_seconds=0 should be honoured as 0, got {budget} "
        f"(likely silently fell back to IDLE_WATCHDOG_SECONDS={IDLE_WATCHDOG_SECONDS})"
    )


def test_reap_reason_idle_stall(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Recover path (no usage limit) sets reap_reason=idle_stall."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="idle-stall-1",
        name="client-a/auto-dev/IDLE-STALL-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="IDLE-STALL-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-stall-1",
                    attempts=1,  # < DEFAULT_IDLE_RETRY_CAP → recover path
                )
            ]
        )
    )

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch(
            "cw.reconcile._shared.detect_usage_limit",
            return_value=UsageLimitDetection(
                detected=False, matched_at=None, transcript_tail_at=None
            ),
        ),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "idle-stall-1")
    assert s.reap_reason == ReapReason.IDLE_STALL


def test_reap_reason_usage_limit_cutoff(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """flag_silently_idle recover branch with usage limit sets
    reap_reason=usage_limit_cutoff."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="usage-limit-1",
        name="client-a/auto-dev/USAGE-LIMIT-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="USAGE-LIMIT-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="usage-limit-1",
                    attempts=1,  # < cap → recover path
                )
            ]
        )
    )

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch(
            "cw.reconcile._shared.detect_usage_limit",
            return_value=UsageLimitDetection(
                detected=True, matched_at=None, transcript_tail_at=None
            ),
        ),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "usage-limit-1")
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def _run_idle_recover_with_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sid: str,
    records: list[dict[str, object]],
) -> Session:
    """Drive the idle recover branch (_revert_task_candidate) against a REAL
    detector + transcript. Returns the reloaded session."""
    home = tmp_path / f"home-{sid}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / f"wt-{sid}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id=sid,
        name=f"client-a/auto-dev/{sid.upper()}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=sid.upper(),
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sid,
                    attempts=1,  # < cap → recover path
                )
            ]
        )
    )
    transcript = _write_transcript_records(
        home, worktree, records, filename="live-ref-sess-1345.jsonl"
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    reloaded = load_state()
    return next(s for s in reloaded.sessions if s.id == sid)


def test_idle_revert_usage_limit_recent_sets_cutoff_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 idle recover branch, real detector: limit at tail → USAGE_LIMIT_CUTOFF."""
    s = _run_idle_recover_with_transcript(
        tmp_path,
        monkeypatch,
        sid="idle-ul-recent",
        records=[
            _ul_record("working on it", "2026-01-01T00:00:10+00:00"),
            _ul_record(
                "You've hit your session limit · resets 5am",
                "2026-01-01T00:00:20+00:00",
            ),
        ],
    )
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_idle_revert_usage_limit_stale_keeps_idle_stall_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345 idle recover branch, real detector: stale limit → IDLE_STALL."""
    s = _run_idle_recover_with_transcript(
        tmp_path,
        monkeypatch,
        sid="idle-ul-stale",
        records=[
            _ul_record(
                "You've hit your session limit · resets 5am",
                "2026-01-01T00:00:10+00:00",
            ),
            # 301s later — beyond the 300s backoff window.
            _ul_record("unrelated later progress", "2026-01-01T00:05:11+00:00"),
        ],
    )
    assert s.reap_reason == ReapReason.IDLE_STALL


def test_reap_reason_retry_cap_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """flag_silently_idle park branch (cap reached) sets
    reap_reason=retry_cap_parked."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    sess = Session(
        id="cap-parked-1",
        name="client-a/auto-dev/CAP-PARKED-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        surface_ref="live-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="CAP-PARKED-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="cap-parked-1",
                    attempts=2,  # at DEFAULT_IDLE_RETRY_CAP → park path
                )
            ]
        )
    )

    with (
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
        patch("cw.reconcile._deps.fire_push_notification"),
    ):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "cap-parked-1")
    assert s.reap_reason == ReapReason.RETRY_CAP_PARKED


def test_detect_idle_candidates_under_budget_returns_empty(
    tmp_config_dir: Path,
) -> None:
    """Session elapsed < idle budget → no candidate."""
    from cw.reconcile import _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    sess = _mk_live_idle_daemon_session("idle-under-1", "live-ref", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=_auto_config(),
        task_by_ticket={},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidates_increment_counter_only(
    tmp_config_dir: Path,
) -> None:
    """Session past budget, not recently active, count=0, threshold=2.

    Expects INCREMENT_COUNTER candidate.
    """
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session("idle-inc-1", "live-ref", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.INCREMENT_COUNTER
    assert c.new_observation_count == 1
    # Purity: bytes unchanged
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidates_counter_not_written_by_detect(
    tmp_config_dir: Path,
) -> None:
    """Explicit purity: after detect, load_state() → idle_observation_count still 0."""
    from cw.reconcile import _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session("idle-nowrit-1", "live-ref", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={},
    )

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "idle-nowrit-1")
    assert s.idle_observation_count == 0


def test_detect_idle_candidates_threshold_reached_park(
    tmp_config_dir: Path,
) -> None:
    """idle_observation_count at threshold-1, task.attempts >= cap.

    Expects PARK_BLOCKED_ON_USER candidate.
    """
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session(
        "idle-park-1", "live-ref", started_at, idle_observation_count=1
    )
    state = CwState(sessions=[sess])
    save_state(state)
    # task.attempts >= cap → park
    task = TicketTask(
        ticket_id="idle-park-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-park-1",
        attempts=99,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    snap = _state_queue_snapshot()

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={"idle-park-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidates_threshold_reached_recover(
    tmp_config_dir: Path,
) -> None:
    """idle_observation_count at threshold-1 + task.attempts < cap → REVERT_TASK."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session(
        "idle-recover-1", "live-ref", started_at, idle_observation_count=1
    )
    state = CwState(sessions=[sess])
    save_state(state)
    # task.attempts=0 < cap=2 → recover
    task = TicketTask(
        ticket_id="idle-recover-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-recover-1",
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    snap = _state_queue_snapshot()

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={"idle-recover-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.REVERT_TASK
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidates_recover_counter_when_liveness_restored(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """idle_observation_count=1 + transcript recently active → RECOVER_COUNTER."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session(
        "idle-recov-cnt-1", "live-ref", started_at, idle_observation_count=1
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    # Patch transcript recently active → True
    monkeypatch.setattr(
        "cw.reconcile.idle._transcript_recently_active", lambda _s, _n, **_kw: True
    )
    monkeypatch.setattr("cw.reconcile.idle._awaiting_subagent", lambda _s, _n: False)

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.RECOVER_COUNTER
    assert c.new_observation_count == 0
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidate_not_recover_counter_on_trailing_metadata_write(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real (unmocked) transcript: a trailing metadata write must not falsely
    RECOVER_COUNTER (#1076, the ticket's 21-hour stranding repro).

    The last content-bearing entry is old (beyond the liveness window); a
    metadata-only record (no "message") is appended last with a fresh mtime.
    Pre-fix, mtime-based liveness would see "recently active" and wrongly
    reset the observation counter.
    """
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-idle-trailing-meta"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    old_ts = now - timedelta(hours=3)

    _write_transcript_records(
        home,
        worktree,
        [
            {
                "type": "assistant",
                "timestamp": old_ts.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "working"}],
                },
            },
            {"type": "ai-title", "title": "Fix the thing"},
        ],
    )

    config = _auto_config(idle_confirm_observations=5)
    sess = _mk_live_idle_daemon_session(
        "idle-trailing-meta-1",
        "fake-short-id",
        started_at,
        idle_observation_count=1,
        worktree_path=worktree,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action != ProposedAction.RECOVER_COUNTER
    assert c.proposed_action == ProposedAction.INCREMENT_COUNTER
    assert c.new_observation_count == 2


def test_detect_idle_candidates_salvage_git(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session at threshold with worktree_path + branch → SALVAGE_GIT."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    wt_path = tmp_path / "wt-salvage-git"
    wt_path.mkdir(parents=True)
    sess = _mk_live_idle_daemon_session(
        "idle-sgit-1",
        "live-ref",
        started_at,
        idle_observation_count=1,
        worktree_path=wt_path,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-sgit-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-sgit-1",
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    snap = _state_queue_snapshot()

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-sgit-1"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={"idle-sgit-1": task},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.SALVAGE_GIT
    assert c.branch == "auto-dev/idle-sgit-1"
    assert _state_queue_snapshot() == snap


def _setup_idle_stage_complete_session(
    home: Path,
    worktree: Path,
    *,
    sid: str,
    started_at: datetime,
    set_park_marker: bool = True,
    write_transcript: bool = True,
) -> Session:
    """Build a confirmed-idle DAEMON session carrying a stage_complete sentinel.

    Reproduces the #1283 precondition for the idle advance-sentinel backstop:
    a session that finished its stage (a ``stage_complete`` transcript) but whose
    ``last_result`` is already a non-``None``, status-free park marker -- which
    closes idle.py's pre-existing ``last_result is None`` unrouted-check fast path
    (#578) so the sentinel reaches ``_classify_idle_threshold``. The transcript
    mtime is pinned strictly after ``started_at`` but far enough before ``now``
    (``started_at + 60s`` against a ``now`` 20 min later) that
    ``_transcript_recently_active`` reports ``False`` and the liveness gate falls
    through to the confirmed-idle classifier. ``_write_salvage_transcript``'s
    record carries no ``"timestamp"`` field, so ``_effective_transcript_timestamp``
    falls back to mtime and ``os.utime`` fully controls the liveness math.
    """
    sess = _mk_headless_daemon_session(sid, worktree, started_at)
    sess.idle_observation_count = 1
    if set_park_marker:
        sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    if write_transcript:
        payload = _stage_complete_payload()
        payload["ticket_id"] = sid
        path = _write_salvage_transcript(home, worktree, f"claude-{sid}", payload)
        ts = (started_at + timedelta(seconds=60)).timestamp()
        os.utime(path, (ts, ts))
    return sess


def test_idle_advance_sentinel_backstop_prevents_salvage_git(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283: a confirmed-idle session with a stage_complete sentinel and a
    non-None park marker routes ROUTE_EMITTED_SENTINEL, not SALVAGE_GIT."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-1"
    sess = _setup_idle_stage_complete_session(
        home, worktree, sid="idle-adv-1", started_at=started_at
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-adv-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-1",
        stage=Stage.IMPL,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-adv-1"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-1": task},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    assert c.routed_sentinel is not None
    assert c.routed_sentinel.status == "stage_complete"


def test_idle_advance_sentinel_backstop_later_stage(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later-stage (task behind the sentinel) stage_complete is also harvested."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-later"
    sess = _setup_idle_stage_complete_session(
        home, worktree, sid="idle-adv-later", started_at=started_at
    )
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="idle-adv-later",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-later",
        # PLAN is EARLIER than the sentinel's mapped IMPL stage -> "later".
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-later": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL


def test_idle_advance_sentinel_backstop_earlier_stage_falls_through(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier-stage (stale replay) sentinel is NOT harvested -> SALVAGE_GIT."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-earlier"
    sess = _setup_idle_stage_complete_session(
        home, worktree, sid="idle-adv-earlier", started_at=started_at
    )
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="idle-adv-earlier",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-earlier",
        # REVIEW is LATER than the sentinel's mapped IMPL stage -> "earlier".
        stage=Stage.REVIEW,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-adv-earlier"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-earlier": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SALVAGE_GIT


def test_idle_advance_sentinel_backstop_no_transcript_falls_through(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No transcript -> unchanged SALVAGE_GIT (backward-compat lock)."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-notx"
    sess = _setup_idle_stage_complete_session(
        home,
        worktree,
        sid="idle-adv-notx",
        started_at=started_at,
        set_park_marker=False,
        write_transcript=False,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-adv-notx",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-notx",
        stage=Stage.IMPL,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-adv-notx"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-notx": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SALVAGE_GIT


def test_idle_advance_sentinel_backstop_finalize_stage_deferred(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FINALIZE-stage session yields no candidate (owned by stalled.py)."""
    from cw.reconcile import _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-finalize"
    sess = _setup_idle_stage_complete_session(
        home, worktree, sid="idle-adv-finalize", started_at=started_at
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-adv-finalize",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-finalize",
        stage=Stage.FINALIZE,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-adv-finalize"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-finalize": task},
    )

    assert candidates == []


def test_stage_complete_git_salvage_candidate_not_produced_end_to_end(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (#1283): flag_silently_idle_daemon_sessions produces no
    git-salvage candidate for a stage_complete session; the task advances."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-adv-e2e"
    sess = _setup_idle_stage_complete_session(
        home, worktree, sid="idle-adv-e2e", started_at=started_at
    )
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="idle-adv-e2e",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-adv-e2e",
        stage=Stage.IMPL,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-adv-e2e"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

    blocked, salvage_git = flag_silently_idle_daemon_sessions(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-adv-e2e": task},
    )

    assert salvage_git == []
    assert blocked == []
    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "idle-adv-e2e"
    )
    assert task_after.stage == Stage.REVIEW
    assert task_after.status == QueueItemStatus.PENDING
    assert task_after.session_id is None


def test_detect_idle_candidates_finalize_yields_zero_candidates(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FINALIZE-stage session at threshold -> zero candidates.

    Defers to stalled.py; see GitHub #1054.
    """
    from cw.reconcile import _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    wt_path = tmp_path / "wt-idle-finalize"
    wt_path.mkdir(parents=True)
    sess = _mk_live_idle_daemon_session(
        "idle-finalize-2",
        "live-ref",
        started_at,
        idle_observation_count=1,
        worktree_path=wt_path,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-finalize-2",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-finalize-2",
        attempts=0,
        stage=Stage.FINALIZE,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    snap = _state_queue_snapshot()

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/idle-finalize-2"
    )
    monkeypatch.setattr("cw.reconcile.idle._detect_post_review_clean", lambda _s: False)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: False
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={"idle-finalize-2": task},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


def test_detect_idle_candidates_worktree_dirty_flag(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dirty worktree → candidate.worktree_dirty == True; bytes unchanged."""
    from cw.reconcile import _detect_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    config = _auto_config(idle_confirm_observations=2)
    sess = _mk_live_idle_daemon_session(
        "idle-dirty-1",
        "live-ref",
        started_at,
        idle_observation_count=1,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    # task.attempts=99 >= cap → PARK path, which checks dirty
    task = TicketTask(
        ticket_id="idle-dirty-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-dirty-1",
        attempts=99,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    snap = _state_queue_snapshot()

    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: True
    )

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"live-ref"},
        config=config,
        task_by_ticket={"idle-dirty-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].worktree_dirty is True
    assert _state_queue_snapshot() == snap


# --- _detect_phantom_candidates ---


def test_act_on_idle_park_routes_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """PARK_BLOCKED_ON_USER candidate → queue BLOCKED_ON_USER."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-act-park-1", "live-ref", started_at, idle_observation_count=1
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-act-park-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-act-park-1",
        attempts=99,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="idle-act-park-1",
        proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
        ticket_id="idle-act-park-1",
        new_observation_count=2,
    )

    _act_on_idle_candidates(state, [candidate], now=now)

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "idle-act-park-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


def test_act_on_idle_increments_observation_counter(
    tmp_config_dir: Path,
) -> None:
    """INCREMENT_COUNTER candidate → session.idle_observation_count updated."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-act-inc-1", "live-ref", started_at, idle_observation_count=0
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidate = ReapCandidate(
        session_id="idle-act-inc-1",
        proposed_action=ProposedAction.INCREMENT_COUNTER,
        ticket_id="idle-act-inc-1",
        new_observation_count=2,
    )

    _act_on_idle_candidates(state, [candidate], now=now)

    # Counter must be written by act
    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "idle-act-inc-1")
    assert s.idle_observation_count == 2


def test_act_on_idle_park_emits_needs_attention_and_sets_reap_reason(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """PARK_BLOCKED_ON_USER → SESSION_NEEDS_ATTENTION emitted + reap_reason set."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-park-evt-1", "live-ref", started_at, idle_observation_count=1
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-park-evt-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-park-evt-1",
        attempts=99,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="idle-park-evt-1",
        proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
        ticket_id="idle-park-evt-1",
        new_observation_count=2,
        lane="idle-park-lane",
    )

    _act_on_idle_candidates(state, [candidate], now=now)

    assert sess.reap_reason == ReapReason.RETRY_CAP_PARKED

    events = read_events(
        consumer="test-idle-park-evt-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].payload["paused_status"] == _SILENTLY_IDLE_REASON
    assert events[0].payload["lane"] == "idle-park-lane"


def test_act_on_idle_candidates_escalate_external_emits_needs_attention_zero_mutation(
    tmp_config_dir: Path,
) -> None:
    """ESCALATE_EXTERNAL_IDLE -> SESSION_NEEDS_ATTENTION with real session_name/
    claude_session_id (not None/empty), push notification fired once, and zero
    Session/TicketTask mutation. RFC 0011 B1 (#1158)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates
    from cw.reconcile._shared import _EXTERNAL_COUNTERPARTY_IDLE_REASON

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = Session(
        id="idle-ext-evt-1",
        name="client-a/auto-dev/idle-ext-evt-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=3,
        claude_session_id="csid-ext-evt-1",
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-ext-evt-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-ext-evt-1",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    session_before = sess.model_copy(deep=True)
    task_before = task.model_copy(deep=True)

    candidate = ReapCandidate(
        session_id="idle-ext-evt-1",
        proposed_action=ProposedAction.ESCALATE_EXTERNAL_IDLE,
        ticket_id="idle-ext-evt-1",
        new_observation_count=4,
        client="client-a",
        paused_status=_EXTERNAL_COUNTERPARTY_IDLE_REASON,
        lane="idle-ext-lane",
    )

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        _act_on_idle_candidates(state, [candidate], now=now)
        mock_notify.assert_called_once_with(sess.name, sess.client)

    events = read_events(
        consumer="test-idle-ext-evt-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["session_name"] == "client-a/auto-dev/idle-ext-evt-1"
    assert payload["claude_session_id"] == "csid-ext-evt-1"
    assert payload["paused_status"] == _EXTERNAL_COUNTERPARTY_IDLE_REASON
    assert payload["lane"] == "idle-ext-lane"

    # Zero mutation: neither Session nor TicketTask state changed.
    assert sess.model_dump() == session_before.model_dump()
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "idle-ext-evt-1")
    assert t.model_dump() == task_before.model_dump()


def test_act_on_idle_candidates_escalate_external_only_candidate_not_dropped(
    tmp_config_dir: Path,
) -> None:
    """ESCALATE_EXTERNAL_IDLE as the sole candidate is not dropped by the
    has_dispositions short-circuit ([], [], []). RFC 0011 B1 (#1158)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates
    from cw.reconcile._shared import _EXTERNAL_COUNTERPARTY_IDLE_REASON

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-ext-only-1", "live-ref", started_at, idle_observation_count=3
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-ext-only-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-ext-only-1",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="idle-ext-only-1",
        proposed_action=ProposedAction.ESCALATE_EXTERNAL_IDLE,
        ticket_id="idle-ext-only-1",
        new_observation_count=4,
        client="client-a",
        paused_status=_EXTERNAL_COUNTERPARTY_IDLE_REASON,
    )

    with patch("cw.reconcile._deps.fire_push_notification"):
        result = _act_on_idle_candidates(state, [candidate], now=now)

    assert result == ([], [], [])

    events = read_events(
        consumer="test-idle-ext-only-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].payload["paused_status"] == _EXTERNAL_COUNTERPARTY_IDLE_REASON


def test_act_on_idle_revert_task_routes_pending_emits_timed_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERT_TASK → queue PENDING + SESSION_TIMED_OUT + daemon stopped."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-revert-evt-1", "live-ref", started_at, idle_observation_count=3
    )
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-revert-evt-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-revert-evt-1",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="idle-revert-evt-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="idle-revert-evt-1",
        elapsed_seconds=1500.0,
        new_observation_count=4,
        usage_limit_detected=False,
    )

    _act_on_idle_candidates(state, [candidate], now=now, config=_auto_config())

    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.reap_reason == ReapReason.IDLE_STALL

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "idle-revert-evt-1")
    assert t.status == QueueItemStatus.PENDING

    events = read_events(
        consumer="test-idle-revert-evt-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    assert events[0].payload["cause"] == "idle_stall_recovered"


def test_act_on_idle_revert_task_usage_limit_detected_sets_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERT_TASK with usage_limit_detected=True → cause=usage_limit_cutoff."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = _mk_live_idle_daemon_session(
        "idle-revert-usage-1", "live-ref", started_at, idle_observation_count=3
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidate = ReapCandidate(
        session_id="idle-revert-usage-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="idle-revert-usage-1",
        elapsed_seconds=1500.0,
        new_observation_count=4,
        usage_limit_detected=True,
    )

    _act_on_idle_candidates(state, [candidate], now=now, config=_auto_config())

    assert sess.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

    events = read_events(
        consumer="test-idle-revert-usage-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    assert events[0].payload["cause"] == "usage_limit_cutoff"


def test_act_on_idle_salvage_git_persists_observation_counter(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """SALVAGE_GIT candidate → idle_observation_count persisted to disk."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    wt = tmp_path / "wt-git-salv"

    sess = _mk_live_idle_daemon_session(
        "idle-salv-git-ctr-1",
        "live-ref",
        started_at,
        idle_observation_count=2,
        worktree_path=wt,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidate = ReapCandidate(
        session_id="idle-salv-git-ctr-1",
        proposed_action=ProposedAction.SALVAGE_GIT,
        ticket_id="idle-salv-git-ctr-1",
        branch="auto-dev/idle-salv-git-ctr-1",
        worktree_path_str=str(wt),
        post_review_clean=False,
        new_observation_count=3,
    )

    _act_on_idle_candidates(state, [candidate], now=now)

    # Counter must be written to disk by act — process restart resilience.
    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "idle-salv-git-ctr-1")
    assert s.idle_observation_count == 3


# ---------------------------------------------------------------------------
# _auto_config helper + ReapPolicy signal_only gate tests (#554)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wall-clock-budget liveness veto (#976)
# ---------------------------------------------------------------------------


def _mk_running_task(ticket_id: str, client: str = "client-a") -> TicketTask:
    task = _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=ticket_id,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    return task


def test_idle_queue_mutations_gh_blocked_stamps_disposition(
    tmp_config_dir: Path,
) -> None:
    """gh_blocked_revert_candidates → BLOCKED_ON_USER + disposition='gh_check_blocked'.

    Regression for the bug fixed in this PR: the branch previously omitted
    disposition, causing parked rows to render '—'.
    """
    from cw.reconcile._shared import ProposedAction, ReapCandidate
    from cw.reconcile.idle import _apply_idle_queue_mutations

    _mk_running_task("idle-gh-disp-1")

    candidate = ReapCandidate(
        session_id="idle-gh-disp-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="idle-gh-disp-1",
    )

    _apply_idle_queue_mutations([], [], [candidate], [], [], {})

    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-gh-disp-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    assert t.disposition == "gh_check_blocked"


def test_idle_queue_mutations_merged_stamps_shipped(
    tmp_config_dir: Path,
) -> None:
    """merged_revert_candidates → COMPLETED + disposition='shipped'."""
    from cw.reconcile._shared import ProposedAction, ReapCandidate
    from cw.reconcile.idle import _apply_idle_queue_mutations

    _mk_running_task("idle-merged-disp-1")

    candidate = ReapCandidate(
        session_id="idle-merged-disp-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="idle-merged-disp-1",
        client="client-a",
    )

    _apply_idle_queue_mutations([], [candidate], [], [], [], {})

    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-merged-disp-1")
    assert t.status == QueueItemStatus.COMPLETED
    assert t.disposition == "shipped"


def test_idle_queue_mutations_park_stamps_paused_status(
    tmp_config_dir: Path,
) -> None:
    """park_candidates → BLOCKED_ON_USER + disposition stamped from paused_status."""
    from cw.reconcile._shared import ProposedAction, ReapCandidate
    from cw.reconcile.idle import _apply_idle_queue_mutations

    _mk_running_task("idle-park-disp-1")

    candidate = ReapCandidate(
        session_id="idle-park-disp-1",
        proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
        ticket_id="idle-park-disp-1",
        paused_status="dirty_worktree",
    )

    _apply_idle_queue_mutations([], [], [], [candidate], [], {})

    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-park-disp-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    assert t.disposition == "dirty_worktree"


def test_idle_queue_mutations_salvage_stamps_disposition(
    tmp_config_dir: Path,
) -> None:
    """salvage_candidates with shipped result → COMPLETED + disposition='shipped'."""
    from cw.reconcile._shared import ProposedAction, ReapCandidate
    from cw.reconcile.idle import _apply_idle_queue_mutations

    _mk_running_task("idle-salv-disp-1")

    result = AutoDevResult.model_validate(
        {
            "schema_version": 4,
            "ticket_id": "idle-salv-disp-1",
            "status": "shipped",
            "stage_reached": "stage5_post_create",
            "scope": {
                "tier": "small",
                "files": 1,
                "lines_estimate": 10,
                "lines_actual": 10,
                "forbidden_touched": False,
            },
            "plan_source": "github_issue_existing",
            "branch": "dev/idle-salv-disp-1",
            "worktree_path": None,
            "fork_point_sha": None,
            "commits": [],
            "pr": {
                "number": 1,
                "url": "https://github.com/user/repo/pull/1",
                "auto_merge": False,
                "base": "main",
            },
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "HIGH",
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": None,
            "next_actions": ["wait_for_ci"],
        }
    )

    candidate = ReapCandidate(
        session_id="idle-salv-disp-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="idle-salv-disp-1",
        salvage_result=result,
    )

    salvaged_result_by_ticket = {"idle-salv-disp-1": result}
    _apply_idle_queue_mutations([], [], [], [], [candidate], salvaged_result_by_ticket)

    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "idle-salv-disp-1")
    assert t.status == QueueItemStatus.COMPLETED
    assert t.disposition == "shipped"


# ---------------------------------------------------------------------------
# _parse_any_sentinel_from_transcript — two-layer fallthrough (#892)
# ---------------------------------------------------------------------------
