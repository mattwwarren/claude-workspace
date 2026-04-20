"""Unit tests for cw.reconcile."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import compute_drift, reconcile


def _mk_session(
    sid: str,
    surface_ref: str | None,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    return Session(
        id=sid,
        name=f"client-a/{sid}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        status=status,
        workspace_path=ClientConfig(
            name="client-a", workspace_path="/tmp/ws"
        ).workspace_path,
        surface_ref=surface_ref,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def test_compute_drift_empty_state_returns_empty_report() -> None:
    adapter = FakeCmuxAdapter()
    state = CwState()
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == []


def test_compute_drift_flags_active_session_with_missing_surface() -> None:
    adapter = FakeCmuxAdapter()  # no surfaces
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == ["s1"]


def test_compute_drift_ignores_backgrounded_completed_and_refless() -> None:
    adapter = FakeCmuxAdapter()
    state = CwState(
        sessions=[
            _mk_session("s-bg", "ref1", status=SessionStatus.BACKGROUNDED),
            _mk_session("s-done", "ref2", status=SessionStatus.COMPLETED),
            _mk_session("s-noref", None, status=SessionStatus.ACTIVE),
        ]
    )
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == []


def test_compute_drift_respects_live_set() -> None:
    adapter = FakeCmuxAdapter()
    live_ref = adapter.spawn("ws", "echo hi")  # registers in live set
    state = CwState(
        sessions=[
            _mk_session("alive", live_ref),
            _mk_session("dead", "gone"),
        ]
    )
    report = compute_drift(state, adapter)
    assert report.phantom_session_ids == ["dead"]


def test_compute_drift_empty_live_set_from_adapter_is_reconciled() -> None:
    """Adapters return empty on backend outage. That's 'no surfaces alive' —
    so everything ACTIVE/IDLE with a surface_ref is phantom. The reconciler
    trusts the adapter; callers who want "don't touch state when backend is
    down" must guard before calling.
    """
    adapter = FakeCmuxAdapter()
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    report = compute_drift(state, adapter)
    assert set(report.phantom_session_ids) == {"s1", "s2"}


def test_reconcile_marks_phantom_completed_crashed(
    tmp_config_dir: Path,
) -> None:
    """reconcile flips phantom sessions to COMPLETED/CRASHED and persists."""
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    save_state(state)

    adapter = FakeCmuxAdapter()
    adapter.spawn("ws", "echo decoy")  # keep live set non-empty to bypass outage guard
    report = reconcile(adapter)

    assert report.phantom_session_ids == ["s1"]
    assert report.phantom_session_names == ["client-a/s1"]
    reloaded = load_state()
    s1 = reloaded.find_by_name_or_id("s1")
    assert s1 is not None
    assert s1.status == SessionStatus.COMPLETED
    assert s1.completed_reason == CompletionReason.CRASHED
    assert s1.completed_at is not None
    assert report.reverted_ticket_ids == []


def test_reconcile_reverts_daemon_session_ticket_to_pending(
    tmp_config_dir: Path,
) -> None:
    """When a DAEMON session for a ticket is phantom, revert its task."""
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-1"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    adapter = FakeCmuxAdapter()
    adapter.spawn("ws", "echo decoy")  # non-empty live set bypasses outage guard
    report = reconcile(adapter)

    assert "TKT-1" in report.reverted_ticket_ids
    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING


def test_reconcile_noop_when_no_phantoms(
    tmp_config_dir: Path,
) -> None:
    adapter = FakeCmuxAdapter()
    live_ref = adapter.spawn("ws", "echo")
    sess = _mk_session("alive", live_ref)
    save_state(CwState(sessions=[sess]))

    report = reconcile(adapter)
    assert report.phantom_session_ids == []
    assert report.reverted_ticket_ids == []


def test_reconcile_refuses_to_mass_reap_on_empty_live_set(
    tmp_config_dir: Path,
) -> None:
    """Transient backend outage: adapter returns empty, reconcile must abort.

    Without this guard, a 5-second cmux/tmux restart during `cw status`
    would mark every ACTIVE session COMPLETED+CRASHED irreversibly.
    """
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    save_state(state)

    adapter = FakeCmuxAdapter()  # empty live set simulates backend outage
    report = reconcile(adapter)

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    assert report.reverted_ticket_ids == []

    reloaded = load_state()
    for sid in ("s1", "s2"):
        s = reloaded.find_by_name_or_id(sid)
        assert s is not None
        assert s.status in {SessionStatus.ACTIVE, SessionStatus.IDLE}
        assert s.completed_reason is None
