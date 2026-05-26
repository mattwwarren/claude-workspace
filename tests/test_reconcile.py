"""Unit tests for cw.reconcile."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import freezegun

if TYPE_CHECKING:
    import pytest
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
from cw.reconcile import (
    HEADLESS_TIMEOUT_SECONDS,
    _claude_agents_json,
    compute_drift,
    reconcile,
    revert_completed_silent_tasks,
    revert_stalled_headless_sessions,
    revert_timed_out_tasks,
)


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


def test_claude_agents_json_parses_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json parses the subprocess output and returns a list."""
    import json as _json

    fake_output = _json.dumps([{"sessionId": "abc12345"}, {"sessionId": "def67890"}])

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            stdout = fake_output
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)
    result = _claude_agents_json()
    assert result == [{"sessionId": "abc12345"}, {"sessionId": "def67890"}]


def test_claude_agents_json_returns_empty_on_non_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json returns [] when daemon output is not a list."""
    import json as _json

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            stdout = _json.dumps({"error": "not a list"})
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)
    result = _claude_agents_json()
    assert result == []


def test_compute_drift_empty_state_returns_empty_report() -> None:
    state = CwState()
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_flags_active_session_with_missing_surface() -> None:
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    report = compute_drift(state, set())
    assert report.phantom_session_ids == ["s1"]


def test_compute_drift_ignores_backgrounded_completed_and_refless() -> None:
    state = CwState(
        sessions=[
            _mk_session("s-bg", "ref1", status=SessionStatus.BACKGROUNDED),
            _mk_session("s-done", "ref2", status=SessionStatus.COMPLETED),
            _mk_session("s-noref", None, status=SessionStatus.ACTIVE),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_respects_live_set() -> None:
    live_ref = "live-short-id"
    state = CwState(
        sessions=[
            _mk_session("alive", live_ref),
            _mk_session("dead", "gone"),
        ]
    )
    report = compute_drift(state, {live_ref})
    assert report.phantom_session_ids == ["dead"]


def test_compute_drift_native_daemon_live_set_counts_as_alive() -> None:
    """A surface_ref present in the native daemon's roster is not phantom.

    Daemon-origin workers spawned via ``claude --bg`` store the short
    Claude session id as ``surface_ref``. Reconcile must consider them
    alive via the native roster even though no multiplexer adapter is used.
    """
    daemon = FakeNativeDaemonClient()
    native_ref = daemon.spawn_bg(cwd=Path("/tmp"), prompt="x")
    native_live = {native_ref}
    state = CwState(
        sessions=[
            _mk_session("native-alive", native_ref),
            _mk_session("native-dead", "no-such-short-id"),
        ]
    )
    report = compute_drift(state, native_live)
    assert report.phantom_session_ids == ["native-dead"]


def test_compute_drift_empty_live_set_from_both_backends_is_reconciled() -> None:
    """Empty live set: every ACTIVE/IDLE session with a surface_ref is
    phantom. The reconciler trusts the backend; callers who want
    "don't touch state when daemon is down" must guard before calling.
    """
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    report = compute_drift(state, set())
    assert set(report.phantom_session_ids) == {"s1", "s2"}


def test_reconcile_marks_phantom_completed_crashed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile flips phantom sessions to COMPLETED/CRASHED and persists."""
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    save_state(state)

    # Non-empty live set bypasses outage guard; "missing-ref" is still not live.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    report = reconcile()

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
    monkeypatch: pytest.MonkeyPatch,
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

    # Non-empty live set bypasses outage guard; "dead-ref" still isn't live.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    report = reconcile()

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
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING
    assert queue.tasks[0].session_id is None


def test_reconcile_noop_when_no_phantoms(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_ref = "alive-short-id"
    sess = _mk_session("alive", live_ref)
    save_state(CwState(sessions=[sess]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": live_ref}],
    )
    report = reconcile()
    assert report.phantom_session_ids == []
    assert report.reverted_ticket_ids == []


def test_reconcile_refuses_to_mass_reap_on_empty_live_set(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon reachable but returns empty list: guard fires, no sessions reaped.

    When ``_claude_agents_json`` returns ``[]`` (daemon running but nothing
    live) and state has ACTIVE/IDLE sessions with surface refs, the outage
    guard fires and reconcile returns without mutating state.
    """
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    save_state(state)

    # Daemon reachable, empty roster → guard fires (daemon_errored=False)
    monkeypatch.setattr("cw.reconcile._claude_agents_json", list)
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    assert report.reverted_ticket_ids == []

    reloaded = load_state()
    for sid in ("s1", "s2"):
        s = reloaded.find_by_name_or_id(sid)
        assert s is not None
        assert s.status in {SessionStatus.ACTIVE, SessionStatus.IDLE}
        assert s.completed_reason is None


def test_reconcile_refuses_to_mass_reap_when_daemon_errors(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon subprocess error: guard fires, no sessions reaped."""
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    save_state(state)

    def _boom() -> list[dict[str, object]]:
        raise subprocess.CalledProcessError(1, ["claude", "agents", "--json"])

    monkeypatch.setattr("cw.reconcile._claude_agents_json", _boom)
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    assert report.reverted_ticket_ids == []

    reloaded = load_state()
    for sid in ("s1", "s2"):
        s = reloaded.find_by_name_or_id(sid)
        assert s is not None
        assert s.status in {SessionStatus.ACTIVE, SessionStatus.IDLE}
        assert s.completed_reason is None


def test_reconcile_with_native_live_proceeds(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty live set from _claude_agents_json bypasses outage guard.

    A phantom session (surface_ref not in live set) is still reaped.
    """
    save_state(CwState(sessions=[_mk_session("dead-native", "missing-short-id")]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    report = reconcile()

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
    monkeypatch: pytest.MonkeyPatch,
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

    # No ACTIVE/IDLE sessions with surface_refs, so outage guard doesn't trip
    # even with an empty live set. Monkeypatch _claude_agents_json to avoid
    # subprocess.run calls in tests.
    monkeypatch.setattr("cw.reconcile._claude_agents_json", list)
    report = reconcile()

    assert "43" in report.reverted_ticket_ids

    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == "43")
    assert task.status == QueueItemStatus.PENDING
    assert task.session_id is None


# ---------------------------------------------------------------------------
# revert_completed_silent_tasks tests
# ---------------------------------------------------------------------------


def _mk_daemon_completed_session(sid: str) -> Session:
    """Build a DAEMON COMPLETED session for silent-revert testing."""
    from cw.models import ClientConfig

    sess = _mk_session(sid, surface_ref=None, status=SessionStatus.COMPLETED)
    sess.origin = SessionOrigin.DAEMON
    sess.workspace_path = ClientConfig(
        name="client-a", workspace_path=Path("/tmp/ws")
    ).workspace_path
    return sess


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
    monkeypatch.setattr("cw.reconcile._claude_agents_json", list)
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
# revert_stalled_headless_sessions tests (GitHub issue #185)
# ---------------------------------------------------------------------------


def _mk_headless_daemon_session(
    sid: str,
    worktree: Path,
    started_at: datetime,
    surface_ref: str | None = "fake-short-id",
) -> Session:
    """Build a headless DAEMON ACTIVE session with a cw-context.json."""
    sess = Session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        surface_ref=surface_ref,
        started_at=started_at,
    )
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "' + sid + '"}'
    )
    return sess


def test_revert_stalled_headless_sessions_transitions_past_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session past budget → TIMED_OUT, task reverted, event emitted."""
    worktree = tmp_path / "wt-stalled"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("stalled-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="stalled-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="stalled-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert "stalled-1" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.completed_reason == CompletionReason.TIMED_OUT
    assert sess.completed_at == now

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "stalled-1")
    assert s.status == SessionStatus.TIMED_OUT

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "stalled-1")
    assert t.status == QueueItemStatus.PENDING
    assert t.session_id is None

    events = read_events(
        consumer="test-stalled-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["session_id"] == "stalled-1"
    assert payload["elapsed_seconds"] >= HEADLESS_TIMEOUT_SECONDS


def test_revert_stalled_headless_sessions_leaves_under_budget_alone(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session under budget → unchanged."""
    worktree = tmp_path / "wt-under"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() < HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("under-budget", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_catches_idle_sessions(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """IDLE headless DAEMON session past budget → TIMED_OUT (not ACTIVE-only)."""
    worktree = tmp_path / "wt-idle"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("idle-stalled", worktree, started_at)
    sess.status = SessionStatus.IDLE
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert reverted == []  # no matching ticket task
    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_headless_sessions_skips_non_daemon(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """USER-origin session past budget → unchanged."""
    worktree = tmp_path / "wt-user"
    worktree.mkdir(parents=True, exist_ok=True)
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text('{"headless": true}')

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="user-sess",
        name="client-a/user-sess",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.USER,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_fail_open_missing_context(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with no cw-context.json → fail-open, not transitioned."""
    worktree = tmp_path / "wt-nocontext"
    worktree.mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT write cw-context.json

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = Session(
        id="no-ctx",
        name="client-a/auto-dev/no-ctx",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_skips_none_worktree_path(
    tmp_config_dir: Path,
) -> None:
    """DAEMON session with worktree_path=None → treated as not headless."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_session("no-wt", surface_ref="some-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.started_at = started_at
    assert sess.worktree_path is None

    state = CwState(sessions=[sess])
    save_state(state)

    reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert reverted == []
    assert sess.status == SessionStatus.ACTIVE


def test_revert_stalled_headless_sessions_stops_daemon_surface(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled session has surface_ref → get_native_daemon_client().stop() called."""
    worktree = tmp_path / "wt-stop"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    daemon = FakeNativeDaemonClient()
    short_id = daemon.spawn_bg(cwd=tmp_path, prompt="seed")
    monkeypatch.setattr("cw.reconcile.get_native_daemon_client", lambda: daemon)

    sess = _mk_headless_daemon_session(
        "stop-me", worktree, started_at, surface_ref=short_id
    )
    state = CwState(sessions=[sess])
    save_state(state)

    revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    assert short_id in daemon.stop_calls


def test_reconcile_includes_stalled_reverts_in_report(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() surfaces stalled-session reverts in ReconcileReport."""
    worktree = tmp_path / "wt-rec"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    daemon = FakeNativeDaemonClient()
    short_id = daemon.spawn_bg(cwd=tmp_path, prompt="seed")

    sess = _mk_headless_daemon_session(
        "rec-stalled", worktree, started_at, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="rec-stalled",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="rec-stalled",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    # After revert_stalled_headless_sessions fires, session becomes TIMED_OUT,
    # so the outage guard won't trip. Monkeypatch to avoid subprocess.run.
    monkeypatch.setattr("cw.reconcile._claude_agents_json", list)
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "rec-stalled" in report.reverted_ticket_ids
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "rec-stalled")
    assert t.status == QueueItemStatus.PENDING
