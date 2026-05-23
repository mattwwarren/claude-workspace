"""Unit tests for cw.reconcile."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cw.cmux import FakeCmuxAdapter
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import compute_drift, reconcile, revert_timed_out_tasks


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
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=surface_ref,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def test_compute_drift_empty_state_returns_empty_report() -> None:
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    state = CwState()
    report = compute_drift(state, adapter, daemon)
    assert report.phantom_session_ids == []


def test_compute_drift_flags_active_session_with_missing_surface() -> None:
    adapter = FakeCmuxAdapter()  # no surfaces
    daemon = FakeNativeDaemonClient()
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    report = compute_drift(state, adapter, daemon)
    assert report.phantom_session_ids == ["s1"]


def test_compute_drift_ignores_backgrounded_completed_and_refless() -> None:
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    state = CwState(
        sessions=[
            _mk_session("s-bg", "ref1", status=SessionStatus.BACKGROUNDED),
            _mk_session("s-done", "ref2", status=SessionStatus.COMPLETED),
            _mk_session("s-noref", None, status=SessionStatus.ACTIVE),
        ]
    )
    report = compute_drift(state, adapter, daemon)
    assert report.phantom_session_ids == []


def test_compute_drift_respects_live_set() -> None:
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    live_ref = adapter.spawn("ws", "echo hi")  # registers in live set
    state = CwState(
        sessions=[
            _mk_session("alive", live_ref),
            _mk_session("dead", "gone"),
        ]
    )
    report = compute_drift(state, adapter, daemon)
    assert report.phantom_session_ids == ["dead"]


def test_compute_drift_native_daemon_live_set_counts_as_alive() -> None:
    """A surface_ref present in the native daemon's roster is not phantom.

    Daemon-origin workers spawned via ``claude --bg`` store the short
    Claude session id as ``surface_ref``. Reconcile must consider them
    alive via the native roster even though the multiplexer adapter has
    no record of them.
    """
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    native_ref = daemon.spawn_bg(cwd=Path("/tmp"), prompt="x")
    state = CwState(
        sessions=[
            _mk_session("native-alive", native_ref),
            _mk_session("native-dead", "no-such-short-id"),
        ]
    )
    report = compute_drift(state, adapter, daemon)
    assert report.phantom_session_ids == ["native-dead"]


def test_compute_drift_empty_live_set_from_both_backends_is_reconciled() -> None:
    """Both backends empty: every ACTIVE/IDLE session with a surface_ref is
    phantom. The reconciler trusts the backends; callers who want
    "don't touch state when backends are down" must guard before calling.
    """
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    report = compute_drift(state, adapter, daemon)
    assert set(report.phantom_session_ids) == {"s1", "s2"}


def test_reconcile_marks_phantom_completed_crashed(
    tmp_config_dir: Path,
) -> None:
    """reconcile flips phantom sessions to COMPLETED/CRASHED and persists."""
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    save_state(state)

    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    adapter.spawn("ws", "echo decoy")  # keep live set non-empty to bypass outage guard
    report = reconcile(adapter, daemon)

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
    daemon = FakeNativeDaemonClient()
    adapter.spawn("ws", "echo decoy")  # non-empty live set bypasses outage guard
    report = reconcile(adapter, daemon)

    assert "TKT-1" in report.reverted_ticket_ids
    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING

    # The emitted SESSION_COMPLETED event must carry ticket_id so the
    # dispatch consumer can mark queue tasks COMPLETED downstream.
    events = read_events(
        consumer="test-reconcile-emits-ticket-id",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].payload.get("ticket_id") == "TKT-1"


def test_reconcile_clears_session_id_on_revert(
    tmp_config_dir: Path,
) -> None:
    """Revert clears the stamped session_id so respawn gets a clean slate.

    If the stale session_id lingered on the reverted task, the next
    dispatch_tick would briefly leave it on a freshly RUNNING task before
    re-stamping with the new session_id, opening a window where a
    last-second event from the OLD session could match. Clearing on
    revert closes the window. See GitHub issue #97.
    """
    sess = _mk_session("sess-old", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-CLEAR"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TKT-CLEAR",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="old-session",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    adapter.spawn("ws", "echo decoy")
    reconcile(adapter, daemon)

    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING
    assert queue.tasks[0].session_id is None


def test_reconcile_noop_when_no_phantoms(
    tmp_config_dir: Path,
) -> None:
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    live_ref = adapter.spawn("ws", "echo")
    sess = _mk_session("alive", live_ref)
    save_state(CwState(sessions=[sess]))

    report = reconcile(adapter, daemon)
    assert report.phantom_session_ids == []
    assert report.reverted_ticket_ids == []


def test_reconcile_refuses_to_mass_reap_on_empty_live_set(
    tmp_config_dir: Path,
) -> None:
    """Transient backend outage: both backends empty, reconcile must abort.

    Without this guard, a 5-second multiplexer restart (or a missing
    native roster) during ``cw status`` would mark every ACTIVE session
    COMPLETED+CRASHED irreversibly.
    """
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    save_state(state)

    adapter = FakeCmuxAdapter()  # empty live set simulates backend outage
    daemon = FakeNativeDaemonClient()  # native roster also empty
    report = reconcile(adapter, daemon)

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    assert report.reverted_ticket_ids == []

    reloaded = load_state()
    for sid in ("s1", "s2"):
        s = reloaded.find_by_name_or_id(sid)
        assert s is not None
        assert s.status in {SessionStatus.ACTIVE, SessionStatus.IDLE}
        assert s.completed_reason is None


def test_compute_drift_flags_idle_session_when_pane_runs_bash() -> None:
    """Key zombie test: IDLE session whose pane command is 'bash' is flagged phantom."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    adapter.set_pane_command(ref, "bash")

    daemon = FakeNativeDaemonClient()
    state = CwState(sessions=[_mk_session("zombie", ref, status=SessionStatus.IDLE)])
    report = compute_drift(state, adapter, daemon)
    assert "zombie" in report.phantom_session_ids


def test_compute_drift_keeps_session_alive_when_pane_runs_cw() -> None:
    """Session whose pane foreground process is 'cw' (or 'claude') is alive."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    # Default command from spawn is "claude" — leave it unchanged.

    daemon = FakeNativeDaemonClient()
    state = CwState(sessions=[_mk_session("alive", ref, status=SessionStatus.IDLE)])
    report = compute_drift(state, adapter, daemon)
    assert "alive" not in report.phantom_session_ids


def test_compute_drift_command_check_empty_does_not_falsely_reap() -> None:
    """When list_live_surface_commands returns {} (failure), do not reap sessions
    that are in list_surfaces — fail-open, no false-positive reaping."""
    adapter = FakeCmuxAdapter()
    ref = adapter.spawn("ws", "claude", "right")
    # Simulate a backend failure via the FakeCmuxAdapter fail-mode flag.
    adapter._commands_fail = True

    daemon = FakeNativeDaemonClient()
    state = CwState(sessions=[_mk_session("maybe-alive", ref)])
    report = compute_drift(state, adapter, daemon)
    # list_surfaces has ref, list_live_surface_commands returned {} → fail-open
    assert "maybe-alive" not in report.phantom_session_ids


def test_reconcile_reaps_bash_pane_zombie_session(
    tmp_config_dir: Path,
) -> None:
    """Full reconcile end-to-end: IDLE session with bash pane is COMPLETED+CRASHED."""
    adapter = FakeCmuxAdapter()
    bash_ref = adapter.spawn("ws", "claude", "right")
    adapter.set_pane_command(bash_ref, "bash")
    # Add a decoy live surface so outage guard doesn't fire.
    _decoy_ref = adapter.spawn("ws", "claude", "right")

    sess = _mk_session("zombie-full", bash_ref, status=SessionStatus.IDLE)
    save_state(CwState(sessions=[sess]))

    daemon = FakeNativeDaemonClient()
    report = reconcile(adapter, daemon)

    assert "zombie-full" in report.phantom_session_ids
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("zombie-full")
    assert s is not None
    assert s.status == SessionStatus.COMPLETED
    assert s.completed_reason == CompletionReason.CRASHED


def test_reconcile_outage_guard_still_fires_on_empty_list_surfaces(
    tmp_config_dir: Path,
) -> None:
    """Outage guard: when list_surfaces returns empty and state has live sessions,
    reconcile returns empty report regardless of list_live_surface_commands content."""
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    save_state(state)

    adapter = FakeCmuxAdapter()  # empty live set → outage guard fires
    daemon = FakeNativeDaemonClient()
    report = reconcile(adapter, daemon)

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []

    reloaded = load_state()
    for sid in ("s1", "s2"):
        s = reloaded.find_by_name_or_id(sid)
        assert s is not None
        assert s.status in {SessionStatus.ACTIVE, SessionStatus.IDLE}


def test_reconcile_with_only_native_live_proceeds(
    tmp_config_dir: Path,
) -> None:
    """Non-empty native live set bypasses the outage guard even when the
    multiplexer is empty.

    The outage guard only fires when *both* backends are empty. After the
    issue #150 migration, dispatched workers register with the native
    daemon and the multiplexer is empty in normal operation — so a
    phantom must still be reaped.
    """
    save_state(CwState(sessions=[_mk_session("dead-native", "missing-short-id")]))

    adapter = FakeCmuxAdapter()  # empty
    daemon = FakeNativeDaemonClient()
    daemon.spawn_bg(cwd=Path("/tmp"), prompt="decoy")  # native side non-empty
    report = reconcile(adapter, daemon)

    assert report.phantom_session_ids == ["dead-native"]


def test_reconcile_timed_out_session_reverts_dev_queue_task_to_pending(
    tmp_config_dir: Path,
) -> None:
    """TIMED_OUT session with a RUNNING TicketTask → task reverted to PENDING.

    This is the backstop for the case where signal_stop crashed after
    writing TIMED_OUT but before reverting the dev-queue task.
    See GitHub issue #176 Layer 1.
    """
    # Seed a TIMED_OUT DAEMON session. Its surface_ref is gone (daemon
    # already stopped it), so the backends report nothing live. reconcile
    # only mutates ACTIVE/IDLE sessions, so this session stays TIMED_OUT.
    timed_out_session = Session(
        id="timed-out-sess",
        name="client-a/auto-dev/42",
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
    save_state(CwState(sessions=[timed_out_session]))

    # RUNNING task stamped with the timed-out session.
    dev_store = DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id="42",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="timed-out-sess",
            )
        ]
    )
    save_dev_queue(dev_store)

    reverted = revert_timed_out_tasks()
    assert reverted == ["42"]

    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == "42")
    assert task.status == QueueItemStatus.PENDING
    assert task.session_id is None


def test_reconcile_timed_out_task_revert_called_during_reconcile(
    tmp_config_dir: Path,
) -> None:
    """reconcile() picks up TIMED_OUT session queue revert automatically.

    Ensures the revert_timed_out_tasks call is wired into the main
    reconcile() function and its result surfaces in ReconcileReport.
    """
    timed_out_session = Session(
        id="timed-out-sess-2",
        name="client-a/auto-dev/43",
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
    save_state(CwState(sessions=[timed_out_session]))

    dev_store = DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id="43",
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id="timed-out-sess-2",
            )
        ]
    )
    save_dev_queue(dev_store)

    # Both backends empty but only TIMED_OUT sessions in state (not ACTIVE/IDLE)
    # so the backend-outage guard doesn't trip.
    adapter = FakeCmuxAdapter()
    daemon = FakeNativeDaemonClient()
    report = reconcile(adapter, daemon)

    assert "43" in report.reverted_ticket_ids

    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == "43")
    assert task.status == QueueItemStatus.PENDING
    assert task.session_id is None
