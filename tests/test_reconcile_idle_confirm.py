"""Unit tests for cw.reconcile.idle — confirm-before-reap and idle-park.

Confirm-before-reap observation counter (incl. subagent-activity recovery),
idle-park needs-attention edge triggers, and the signal-only / per-lane idle
act dispatchers. Split out of test_reconcile_idle.py per R5.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cw.config import (
    load_state,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
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
    flag_silently_idle_daemon_sessions,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _mk_headless_daemon_session,
    _mk_live_idle_daemon_session,
    _shipped_salvage_payload,
    _write_salvage_transcript,
)
from tests.conftest import (
    _write_idle_transcript,
)


def test_confirm_before_reap_first_observation_no_disposition(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """First failed idle observation increments counter to 1, no disposition.

    Queue task stays RUNNING; session stays ACTIVE; idle_observation_count==1
    is persisted to disk so a process restart doesn't replay the observation
    as fresh. (#545)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="cbreap-1",
        name="client-a/auto-dev/CBREAP-1",
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
        ticket_id="CBREAP-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cbreap-1",
        attempts=2,  # at cap — would park on pre-#545 single observation
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        blocked, salvage_git = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(),  # default idle_confirm_observations=2
        )
        mock_notify.assert_not_called()

    # No disposition yet — counter is 1, threshold is 2.
    assert blocked == []
    assert salvage_git == []
    assert sess.status == SessionStatus.ACTIVE
    assert sess.idle_observation_count == 1

    # Counter must be persisted: load fresh state and verify.
    reloaded = next(s for s in load_state().sessions if s.id == "cbreap-1")
    assert reloaded.idle_observation_count == 1

    # Queue task untouched.
    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "CBREAP-1")
    assert t.status == QueueItemStatus.RUNNING
    assert t.attempts == 2  # attempts must NOT be incremented


def test_confirm_before_reap_second_observation_fires(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Second consecutive failed observation (counter reaches threshold) fires park.

    SESSION_NEEDS_ATTENTION emitted; task → BLOCKED_ON_USER. Downstream
    semantics unchanged from pre-#545 single-observation behavior. (#545)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    # Session pre-primed at idle_observation_count=1 (first tick already ran).
    sess = Session(
        id="cbreap-2",
        name="client-a/auto-dev/CBREAP-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=1,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="CBREAP-2",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cbreap-2",
        attempts=2,  # at cap → park path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(),  # idle_confirm_observations=2
        )
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "CBREAP-2" in blocked
    # Park: flag-only (#348) — session stays ACTIVE.
    assert sess.status == SessionStatus.ACTIVE
    assert sess.last_result == {"paused_status": _SILENTLY_IDLE_REASON}

    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "CBREAP-2")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    # task.attempts is unchanged — it increments only at CLAIM time.
    assert t.attempts == 2

    events = read_events(
        consumer="test-cbreap-2",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].payload["session_id"] == "cbreap-2"


# ---------------------------------------------------------------------------
# SESSION_NEEDS_ATTENTION idle park edge-trigger — fires once, suppressed on
# re-park ticks (GitHub #782, Source B)
# ---------------------------------------------------------------------------


def test_idle_park_session_needs_attention_fires_only_on_first_park(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Idle park: SESSION_NEEDS_ATTENTION fires once, suppressed on re-park ticks."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="park-once",
        name="client-a/auto-dev/park-once",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-park-once",
        started_at=started_at,
        idle_observation_count=1,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="park-once",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="park-once",
        attempts=2,  # at cap → park path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    with patch("cw.reconcile._deps.fire_push_notification", _capture_push):
        # Tick 1: fresh park — event and push must fire.
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-once"},
            config=_auto_config(),
        )
        # Tick 2: session still ACTIVE (park is flag-only), re-detected as park
        # candidate — event and push must NOT re-fire.
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-once"},
            config=_auto_config(),
        )

    events = read_events(
        consumer="test-park-once",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    matching = [e for e in events if e.payload.get("session_id") == "park-once"]
    assert len(matching) == 1, (
        f"SESSION_NEEDS_ATTENTION must fire exactly once but got {len(matching)}"
    )
    assert len(push_calls) == 1, (
        f"fire_push_notification must be called exactly once but got {len(push_calls)}"
    )


def test_idle_park_push_notification_suppressed_on_re_park(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Re-park tick: fire_push_notification not called when session already parked."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="park-push",
        name="client-a/auto-dev/park-push",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-park-push",
        started_at=started_at,
        idle_observation_count=1,
        # Pre-set last_result to simulate a prior tick having already parked.
        last_result={"paused_status": _SILENTLY_IDLE_REASON},
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="park-push",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="park-push",
        attempts=2,  # at cap
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    with patch("cw.reconcile._deps.fire_push_notification", _capture_push):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-push"},
            config=_auto_config(),
        )

    assert push_calls == [], (
        "fire_push_notification must not fire on re-park tick (session already parked)"
    )

    events = read_events(
        consumer="test-park-push",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    matching = [e for e in events if e.payload.get("session_id") == "park-push"]
    assert len(matching) == 0, (
        "SESSION_NEEDS_ATTENTION must not re-fire on re-park tick"
    )


def test_idle_park_new_session_fires_while_already_parked_suppressed(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Mixed: new park fires; already-parked session suppressed in same tick."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    # Fresh session — first park.
    sess_new = Session(
        id="park-mix-new",
        name="client-a/auto-dev/park-mix-new",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-park-mix-new",
        started_at=started_at,
        idle_observation_count=1,
    )
    # Already-parked session — re-park tick.
    sess_old = Session(
        id="park-mix-old",
        name="client-a/auto-dev/park-mix-old",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-park-mix-old",
        started_at=started_at,
        idle_observation_count=1,
        last_result={"paused_status": _SILENTLY_IDLE_REASON},
    )
    state = CwState(sessions=[sess_new, sess_old])
    save_state(state)

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="park-mix-new",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="park-mix-new",
                    attempts=2,
                ),
                TicketTask(
                    ticket_id="park-mix-old",
                    client="client-a",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id="park-mix-old",
                    attempts=2,
                ),
            ]
        )
    )

    push_calls: list[str] = []

    def _capture_push(name: str, _client: str, **_kw: object) -> None:
        push_calls.append(name)

    with patch("cw.reconcile._deps.fire_push_notification", _capture_push):
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-mix-new", "live-park-mix-old"},
            config=_auto_config(),
        )

    events = read_events(
        consumer="test-park-mix",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    fired_sids = {e.payload.get("session_id") for e in events}
    assert "park-mix-new" in fired_sids, "New park must fire SESSION_NEEDS_ATTENTION"
    assert "park-mix-old" not in fired_sids, (
        "Already-parked session must NOT re-fire SESSION_NEEDS_ATTENTION"
    )
    assert any("park-mix-new" in n for n in push_calls), (
        "fire_push_notification must fire for new park"
    )
    assert all("park-mix-old" not in n for n in push_calls), (
        "fire_push_notification must not fire for already-parked session"
    )


def test_idle_park_re_fires_after_paused_status_cleared(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Re-park re-fires after paused_status cleared — guard is stateless, not permanent.

    Drives reconcile: idle-park (assert SESSION_NEEDS_ATTENTION + fire_push_notification
    fire) → clear paused_status → re-park, assert both re-fire. (GitHub #827)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="park-refire",
        name="client-a/auto-dev/park-refire",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-park-refire",
        started_at=started_at,
        idle_observation_count=1,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="park-refire",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="park-refire",
        attempts=2,  # at cap → park path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    with patch("cw.reconcile._deps.fire_push_notification", _capture_push):
        # Tick 1: fresh park — SESSION_NEEDS_ATTENTION and push must fire.
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-refire"},
            config=_auto_config(),
        )
        assert sess.last_result == {"paused_status": _SILENTLY_IDLE_REASON}
        assert len(push_calls) == 1, "fire_push_notification must fire on first park"

        # Clear paused_status — simulate the marker being removed (e.g. operator
        # intervention or reconcile clearing it).  The guard reads live state each
        # tick, so clearing the marker must allow re-emission on the next park.
        sess.last_result = None
        save_state(state)

        # Tick 2: paused_status cleared → guard must not suppress; both must re-fire.
        flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-park-refire"},
            config=_auto_config(),
        )

    assert len(push_calls) == 2, "fire_push_notification must re-fire after clear"
    events = read_events(
        consumer="test-park-refire",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    matching = [e for e in events if e.payload.get("session_id") == "park-refire"]
    assert len(matching) == 2, "SESSION_NEEDS_ATTENTION must re-fire after clear"


def test_confirm_before_reap_liveness_recovery_resets_counter(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Liveness recovery between observations resets counter to 0 and persists.

    Subsequent idleness starts counting from 1 again. (#545)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="cbreap-3",
        name="client-a/auto-dev/CBREAP-3",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=1,  # counter was 1 from a prior failed observation
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="CBREAP-3",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="cbreap-3",
                    attempts=1,
                )
            ]
        )
    )

    # Liveness check returns True (worker is alive this tick).
    with patch(
        "cw.reconcile.idle._detect._transcript_recently_active", return_value=True
    ):
        blocked, salvage_git = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(),
        )

    assert blocked == []
    assert salvage_git == []
    # Counter reset to 0 and persisted.
    assert sess.idle_observation_count == 0
    reloaded = next(s for s in load_state().sessions if s.id == "cbreap-3")
    assert reloaded.idle_observation_count == 0

    # A new idle observation now starts the count at 1 (not 2).
    with patch(
        "cw.reconcile.idle._detect._transcript_recently_active", return_value=False
    ):
        blocked2, _ = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(),  # threshold=2
        )

    assert blocked2 == []  # still only at count 1, threshold not reached
    assert sess.idle_observation_count == 1


def test_confirm_before_reap_sentinel_salvage_not_deferred(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel salvage dispositions on first observation — counter does not gate it.

    Evidence-based completion is not deferred by confirm-before-reap. (#545)
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-sentinel-nodefer"

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    # idle_observation_count=0 — first observation.
    sess = _mk_headless_daemon_session("nodefer-sent", worktree, started_at)
    sess.idle_observation_count = 0
    _write_salvage_transcript(
        home, worktree, "nodefer-uuid", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="nodefer-sent",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="nodefer-sent",
                    attempts=0,
                    # Matches _shipped_salvage_payload()'s stage_reached
                    # ("stage5_post_create") so the #1019/#1031 stage-match
                    # guard accepts the route.
                    stage=Stage.FINALIZE,
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon),
        patch(
            "cw.reconcile.idle._detect._transcript_recently_active", return_value=False
        ),
        patch("cw.reconcile.idle._detect._awaiting_subagent", return_value=False),
    ):
        blocked, _salvage_git = flag_silently_idle_daemon_sessions(
            state=CwState(sessions=[sess]),
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),  # idle_confirm_observations=2
        )

    # Sentinel salvage fires immediately — not deferred to observation 2.
    assert blocked == []
    assert sess.status == SessionStatus.COMPLETED
    assert sess.completed_reason == CompletionReason.NORMAL


def test_confirm_before_reap_git_salvage_deferred(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-salvage candidate NOT collected on first observation; collected at threshold.

    Git-salvaging a quiet-but-healthy worker would PR half-done work, so it is
    gated like the reap. (#545)
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-git-deferred"
    worktree.mkdir(parents=True)

    sess = Session(
        id="git-deferred",
        name="client-a/auto-dev/GIT-DEFERRED",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=0,  # first observation
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GIT-DEFERRED",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="git-deferred",
                    attempts=5,  # above cap — would normally go to salvage_git
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "dev/git-deferred-branch",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.salvage_terminal_result", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        "cw.reconcile.idle._detect._transcript_recently_active",
        lambda *_a, **_kw: False,
    )
    monkeypatch.setattr(
        "cw.reconcile.idle._detect._awaiting_subagent", lambda *_a, **_kw: False
    )

    state = CwState(sessions=[sess])

    # First observation — git-salvage must NOT be collected yet.
    _, salvage_git_1 = flag_silently_idle_daemon_sessions(
        state,
        now=now,
        native_live={"live-ref"},
        config=_auto_config(),  # idle_confirm_observations=2
    )
    assert salvage_git_1 == []
    assert sess.idle_observation_count == 1

    # Second observation — threshold reached, git-salvage collected.
    _, salvage_git_2 = flag_silently_idle_daemon_sessions(
        state,
        now=now,
        native_live={"live-ref"},
        config=_auto_config(),
    )
    assert len(salvage_git_2) == 1
    assert salvage_git_2[0][0] == "git-deferred"


def test_confirm_before_reap_park_deferred(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Park path (attempts >= cap) deferred until threshold. (#545)"""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="park-defer",
        name="client-a/auto-dev/PARK-DEFER",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=0,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="PARK-DEFER",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="park-defer",
                    attempts=2,  # at cap → park when threshold reached
                )
            ]
        )
    )

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        blocked, _ = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(),  # idle_confirm_observations=2
        )
        mock_notify.assert_not_called()

    assert blocked == []
    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "PARK-DEFER")
    assert t.status == QueueItemStatus.RUNNING
    # attempts unchanged
    assert t.attempts == 2


def test_confirm_before_reap_attempts_unchanged(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """task.attempts is unchanged by any number of idle observations. (#545)

    CRITICAL: task.attempts increments only at CLAIM time in dispatch.py.
    Conflating it with idle observations would permanently park tasks when
    DEFAULT_IDLE_RETRY_CAP=2.
    """
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    sess = Session(
        id="att-unchanged",
        name="client-a/auto-dev/ATT-UNCHANGED",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=0,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="ATT-UNCHANGED",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="att-unchanged",
                    attempts=1,
                )
            ]
        )
    )

    # Run three observations — one counter-only, then threshold fires recover.
    config = _auto_config(idle_confirm_observations=2)
    with patch("cw.reconcile._deps.get_native_daemon_client", return_value=MagicMock()):
        # First observation: no disposition.
        flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=config
        )
        first = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "ATT-UNCHANGED"
        )
        assert first.attempts == 1

        # Second observation: recover fires (attempts=1 < cap=2).
        flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=config
        )
        second = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "ATT-UNCHANGED"
        )
        # still unchanged — only dispatch.py increments this
        assert second.attempts == 1


def test_confirm_before_reap_v5_state_round_trips(
    tmp_config_dir: Path,
) -> None:
    """v5 state payload (no idle_observation_count key) round-trips through load.

    Counter defaults 0; schema_version stamped to current after migration.
    (#545, #380, #555)
    """

    state_dir = tmp_config_dir / ".local" / "share" / "cw"
    sf = state_dir / "sessions.json"
    sf.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "sessions": [
                    {
                        "id": "v5-sess",
                        "name": "c/impl",
                        "client": "c",
                        "purpose": "impl",
                        "workspace_path": str(state_dir),
                        # No idle_observation_count key — v5 did not have it.
                    }
                ],
            }
        )
    )
    loaded = load_state()
    assert loaded.schema_version == CW_STATE_SCHEMA_VERSION
    assert loaded.sessions[0].idle_observation_count == 0


def test_confirm_before_reap_single_observation_config(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """idle_confirm_observations=1 reproduces pre-#545 single-observation behavior."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > IDLE_WATCHDOG_SECONDS

    sess = Session(
        id="single-obs",
        name="client-a/auto-dev/SINGLE-OBS",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref="live-ref",
        started_at=started_at,
        idle_observation_count=0,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="SINGLE-OBS",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="single-obs",
                    attempts=2,  # at cap → park
                )
            ]
        )
    )

    with patch("cw.reconcile._deps.fire_push_notification") as mock_notify:
        blocked, _ = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )
        mock_notify.assert_called_once()

    assert "SINGLE-OBS" in blocked
    t = next(t for t in load_dev_queue().tasks if t.ticket_id == "SINGLE-OBS")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


# resolve_idle_watchdog_budget + per-tier/per-ticket override tests (#326)
# ---------------------------------------------------------------------------


def test_idle_park_candidate_stamps_silently_idle_paused_status(
    tmp_config_dir: Path,
) -> None:
    """_classify_idle_threshold's PARK_BLOCKED_ON_USER carries paused_status (#976)."""
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, ProposedAction
    from cw.reconcile.idle import _classify_idle_threshold

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = Session(
        id="idle-park-disp-1",
        name="client-a/auto-dev/idle-park-disp-1",
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
        ticket_id="idle-park-disp-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-park-disp-1",
        attempts=DEFAULT_IDLE_RETRY_CAP,
    )

    candidate = _classify_idle_threshold(
        sess,
        task=task,
        ticket_id="idle-park-disp-1",
        config=_auto_config(),
        elapsed=1000.0,
        new_count=3,
    )

    assert candidate.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    assert candidate.paused_status == _SILENTLY_IDLE_REASON


def test_idle_confirm_recovers_via_subagent_transcript_activity(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 end-to-end: a stale registered transcript + a fresh subagent sibling
    recovers the idle counter (RECOVER_COUNTER) instead of proceeding to reap."""
    from cw.reconcile import ProposedAction, _detect_idle_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=20)
    config = _auto_config(idle_confirm_observations=2)
    worktree = tmp_path / "wt-idle-recover"

    sess = _mk_headless_daemon_session("idle-recover-1", worktree, started_at)
    sess.idle_observation_count = 1
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="idle-recover-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="idle-recover-1",
        stage=Stage.IMPL,
        attempts=0,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    sib = _write_idle_transcript(home, worktree, filename="subagent-live.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={"idle-recover-1": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.RECOVER_COUNTER


# ---------------------------------------------------------------------------
# session.phantom_reverted event tests (GitHub issue #459)
# ---------------------------------------------------------------------------


class TestActOnIdleCandidatesSignalOnly:
    """Under signal_only policy, REVERT_TASK idle candidates → BLOCKED_ON_USER."""

    def test_signal_only_routes_idle_revert_to_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal_only: idle REVERT_TASK → BLOCKED_ON_USER; daemon stop NOT called."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_live_idle_daemon_session(
            "so-idle-1", "live-ref", started_at, idle_observation_count=2
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="so-idle-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="so-idle-1",
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="so-idle-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="so-idle-1",
            elapsed_seconds=1000.0,
            new_observation_count=2,
        )

        # signal_only is default
        blocked, _, _salvage = _act_on_idle_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        assert blocked == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "so-idle-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == ReapReason.IDLE_STALL.value
        # session_id preserved (not cleared) — operator review traceability
        assert t.session_id == "so-idle-1"
        # Daemon stop NOT called
        assert daemon.stop_calls == []

    def test_signal_only_park_candidates_pass_through(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PARK_BLOCKED_ON_USER is not gated — still processes under signal_only."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )

        sess = _mk_live_idle_daemon_session(
            "so-idle-park-1", "live-ref", started_at, idle_observation_count=2
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="so-idle-park-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="so-idle-park-1",
            attempts=99,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="so-idle-park-1",
            proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
            ticket_id="so-idle-park-1",
            new_observation_count=2,
        )

        blocked, _, _salvage = _act_on_idle_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        # PARK_BLOCKED_ON_USER passes through; task is BLOCKED_ON_USER
        assert "so-idle-park-1" in blocked
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "so-idle-park-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_auto_policy_idle_still_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUTO policy: idle REVERT_TASK still routes to PENDING."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_live_idle_daemon_session(
            "auto-idle-1", "live-ref", started_at, idle_observation_count=2
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="auto-idle-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="auto-idle-1",
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="auto-idle-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="auto-idle-1",
            elapsed_seconds=1000.0,
            new_observation_count=2,
        )

        blocked, _, _salvage = _act_on_idle_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert blocked == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "auto-idle-1")
        assert t.status == QueueItemStatus.PENDING


class TestActOnIdleCandidatesPerLane:
    """Per-lane reap_policy overrides global for idle candidates."""

    def test_lane_auto_global_signal_idle_acts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane AUTO + global SIGNAL_ONLY: idle REVERT_TASK → PENDING."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_idle_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )
        _fast_client_idle = _client_with_lane(
            "client-a", "fast", ReapPolicy.AUTO, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _fast_client_idle},
        )

        sess = _mk_live_idle_daemon_session(
            "lane-auto-idle-1", "live-ref", started_at, idle_observation_count=2
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-auto-idle-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-auto-idle-1",
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-auto-idle-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-auto-idle-1",
            elapsed_seconds=1000.0,
            new_observation_count=2,
            lane="fast",
            client="client-a",
        )

        blocked, _, _salvage = _act_on_idle_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        assert blocked == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-auto-idle-1")
        assert t.status == QueueItemStatus.PENDING

    def test_lane_signal_global_auto_idle_signals(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane SIGNAL_ONLY + global AUTO: idle REVERT_TASK → BLOCKED_ON_USER."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_idle_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        _slow_client_idle = _client_with_lane(
            "client-a", "slow", ReapPolicy.SIGNAL_ONLY, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _slow_client_idle},
        )

        sess = _mk_live_idle_daemon_session(
            "lane-sig-idle-1", "live-ref", started_at, idle_observation_count=2
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-sig-idle-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-sig-idle-1",
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-sig-idle-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-sig-idle-1",
            elapsed_seconds=1000.0,
            new_observation_count=2,
            lane="slow",
            client="client-a",
        )

        blocked, _, _salvage = _act_on_idle_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert blocked == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-sig-idle-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
