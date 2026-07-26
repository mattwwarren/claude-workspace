"""Incident-replay validation suite for loop telemetry (#462).

One fixture per known incident. Each reconstructs the event/state that
characterised the incident and asserts the specific telemetry signal that
MUST light up. These tests are the acceptance bar for the #459/#460
observability batch — if a fixture cannot make an incident visible, the
telemetry is incomplete.

Incidents covered:
  #419 — dispatch stall on stale main (loop-health WARN)
  #421 — phantom revert of a dirty worktree (session.phantom_reverted)
  #418 — suppressed salvage of silently_idle session (session.salvage_skipped)
  #315 — TIMED_OUT session whose branch PR was merged (timed_out-merged WARN)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import freezegun

if TYPE_CHECKING:
    import pytest

from cw.config import save_state
from cw.dev_queue import save_dev_queue
from cw.events import read_events, record_event
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import (
    _SILENTLY_IDLE_REASON,
    reconcile,
    revert_stalled_headless_sessions,
)
from tests.conftest import _make_daemon_session as _cw_make_daemon_session


def _make_daemon_session(
    sid: str,
    ticket_id: str,
    client: str = "client-a",
    surface_ref: str | None = "agent-dead",
    *,
    workspace_path: Path | None = None,
    started_at: datetime | None = None,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """Build a minimal DAEMON-origin session for replay fixtures."""
    return _cw_make_daemon_session(
        id=sid,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        status=status,
        workspace_path=(
            workspace_path if workspace_path is not None else Path("/tmp/ws")
        ),
        surface_ref=surface_ref,
        worktree_path=None,
        started_at=started_at or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# #419 — Dispatch stall on stale main
# ---------------------------------------------------------------------------


@freezegun.freeze_time("2026-06-05T12:00:00Z")
def test_incident_419_dispatch_stall_warns(tmp_config_dir: Path) -> None:
    """Replay #419: 3 consecutive freshness_gate ticks → loop-health WARNs.

    Incident: cw's main branch was behind origin/main, causing every
    dispatch.tick to carry skip_reason=freshness_gate with pending>0
    and running=0. The stall was invisible until _check_loop_health (#460)
    was added.
    """
    from cw.doctor import _check_loop_health

    stall_tick = {
        "client": "client-stall",
        "pending": 2,
        "running": 0,
        "claimed": 0,
        "cap": 3,
        "skip_reason": DispatchSkipReason.FRESHNESS_GATE,
    }
    for _ in range(3):
        record_event(OrchestratorEventType.DISPATCH_TICK, stall_tick)

    results = _check_loop_health()

    warn = [r for r in results if r.warn]
    assert len(warn) == 1, f"Expected 1 warn result, got {warn}"
    assert "client-stall" in warn[0].name
    assert "stalled" in warn[0].detail


# ---------------------------------------------------------------------------
# #421 — Phantom revert of a dirty worktree
# ---------------------------------------------------------------------------


def test_incident_421_phantom_dirty_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay #421: phantom dirty worktree routes ticket to BLOCKED_ON_USER.

    Incident: a DAEMON session disappeared from the daemon roster while its
    worktree still had uncommitted changes. reconcile() reverted the ticket to
    PENDING, losing work silently.

    Fix: dirty worktrees are now routed to BLOCKED_ON_USER for operator
    inspection. The session.phantom_reverted event carries queue_status=
    'blocked_on_user' so the operator can see the decision.

    Dirtiness is driven through worktree_path (not session.branch, which is
    always None for DAEMON sessions — the root cause of the original bug).
    """
    wt_path = tmp_path / "wt-incident-421"
    sess = _make_daemon_session("phantom-421", "TICKET-421", surface_ref="dead-agent")
    sess.worktree_path = wt_path
    sess.branch = None  # Confirms the fix doesn't rely on session.branch
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="TICKET-421",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
    )
    # Non-empty live set bypasses outage guard; "dead-agent" is still not live.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    # Incident #421: worktree was dirty at the time of phantom revert.
    # Drive dirtiness through worktree_path (not session.branch).
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICKET-421",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    reconcile()

    events = read_events(
        consumer="test-incident-421",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1, f"Expected 1 phantom_reverted event, got {events}"
    payload = events[0].payload
    assert payload["session_id"] == "phantom-421"
    assert payload["ticket_id"] == "TICKET-421"
    assert payload["worktree_dirty"] is True
    assert payload["queue_status"] == "blocked_on_user"
    assert events[0].correlation_id == "TICKET-421"

    # The ticket must be BLOCKED_ON_USER, preserving the worktree for operator
    # inspection instead of silently re-dispatching and clobbering the work.
    from cw.dev_queue import load_dev_queue

    queue = load_dev_queue()
    task = next(t for t in queue.tasks if t.ticket_id == "TICKET-421")
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"Expected BLOCKED_ON_USER, got {task.status} — "
        "dirty worktree was silently clobbered"
    )


# ---------------------------------------------------------------------------
# #418 — Suppressed salvage of silently_idle session
# ---------------------------------------------------------------------------

_FROZEN_418 = "2026-06-05T12:00:00Z"
_NOW_418 = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
_STARTED_LONG_AGO = datetime(2026, 6, 5, 0, 0, 0, tzinfo=UTC)  # 12 h before frozen now


@freezegun.freeze_time(_FROZEN_418)
def test_incident_418_silently_idle_emits_salvage_skipped(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay #418: silently_idle park-marker session emits session.salvage_skipped.

    Incident: a headless DAEMON session that was parked by
    flag_silently_idle_daemon_sessions (last_result has paused_status=silently_idle)
    was being re-timed-out by revert_stalled_headless_sessions instead of being
    left alone. The SESSION_SALVAGE_SKIPPED event (#459) makes this path visible.
    """
    sess = _make_daemon_session(
        "silently-idle-418", "TICKET-418", started_at=_STARTED_LONG_AGO
    )
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    monkeypatch.setattr("cw.reconcile.stalled._detect._is_headless", lambda *_: True)

    now = _NOW_418
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-incident-418-skip",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1, f"Expected 1 salvage_skipped event, got {events}"
    payload = events[0].payload
    assert payload["session_id"] == "silently-idle-418"
    assert payload.get("paused_status") == _SILENTLY_IDLE_REASON


@freezegun.freeze_time(_FROZEN_418)
def test_incident_418_terminal_sentinel_no_salvage_skip(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrast: terminal-sentinel last_result does NOT emit salvage_skipped.

    The salvage_skipped path is gated on paused_status==silently_idle. A session
    that genuinely shipped (last_result.status=shipped) must not emit the signal —
    confusing a shipped session with a parked one is a correctness bug.
    """
    sess = _make_daemon_session(
        "shipped-418", "TICKET-418B", started_at=_STARTED_LONG_AGO
    )
    # Real terminal sentinel — no paused_status key.
    sess.last_result = {"status": "shipped", "schema_version": 4}
    state = CwState(sessions=[sess])
    monkeypatch.setattr("cw.reconcile.stalled._detect._is_headless", lambda *_: True)

    now = _NOW_418
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-incident-418-no-skip",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 0, (
        "Expected no salvage_skipped events for terminal-sentinel session, "
        f"got {events}"
    )


# ---------------------------------------------------------------------------
# #315 — TIMED_OUT session whose branch PR was merged
# ---------------------------------------------------------------------------


@freezegun.freeze_time("2026-06-05T12:00:00Z")
def test_incident_315_timed_out_merged_warns(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay #315: TIMED_OUT session with MERGED PR → timed_out-merged WARNs.

    Incident: session e4b91b97 timed out but PR #311 had already merged.
    The work was done; the session was just flagged as failed. The
    _check_timed_out_merged check (#460) surfaces this via a doctor WARN.
    """
    from cw.doctor import _check_timed_out_merged

    sess = _make_daemon_session(
        "timed-315",
        "TICKET-315",
        status=SessionStatus.TIMED_OUT,
    )
    # completed_at must be within the 7-day lookback window.
    sess.completed_at = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)  # 1 day ago
    state = CwState(sessions=[sess])

    monkeypatch.setattr(
        "cw.doctor.loop_health.pr_is_merged_for_ticket",
        lambda *_args, **_kwargs: (True, True),
    )

    results = _check_timed_out_merged(state, {})

    warn = [r for r in results if r.warn]
    assert len(warn) == 1, f"Expected 1 warn result, got {warn}"
    assert "timed-315" in warn[0].detail
    assert "MERGED" in warn[0].detail
    assert "#315" in warn[0].detail
