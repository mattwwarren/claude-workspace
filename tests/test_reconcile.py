"""Unit tests for cw.reconcile."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import freezegun
import pytest

from cw._util import claude_project_dir
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
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
    _SALVAGE_SKIP_REASON,
    _SILENTLY_IDLE_REASON,
    HEADLESS_TIMEOUT_SECONDS,
    IDLE_WATCHDOG_SECONDS,
    SPAWN_GRACE_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _claude_agents_json,
    complete_timed_out_merged_tasks,
    compute_drift,
    flag_silently_idle_daemon_sessions,
    reconcile,
    resolve_headless_budget,
    resolve_idle_watchdog_budget,
    revert_completed_silent_tasks,
    revert_stalled_headless_sessions,
    revert_timed_out_tasks,
)


def _mk_session(
    sid: str,
    surface_ref: str | None,
    status: SessionStatus = SessionStatus.ACTIVE,
    started_at: datetime | None = None,
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
        started_at=(
            started_at if started_at is not None else datetime(2026, 4, 19, tzinfo=UTC)
        ),
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


def test_reconcile_matches_short_id_against_full_uuid_session_id(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real `claude agents --json` returns sessionId as a full UUID; cw's
    surface_ref is the 8-char short id. Reconcile must normalize by slicing
    the UUID to its first 8 chars so the live-set comparison matches.

    Regression test for the second bug in #271 — the FakeNativeDaemonClient
    returns short ids, masking the mismatch in compute_drift unit tests.
    Without this fix, every real daemon session looks phantom and gets
    reaped right after the spawn grace window expires.
    """
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]

    # Session in cw state with the short-id surface_ref (Phase C format).
    state = CwState(sessions=[_mk_session("alive-with-uuid-daemon", short_id)])
    save_state(state)

    # Real daemon shape: sessionId is the full UUID.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": full_uuid}],
    )

    report = reconcile()
    assert report.phantom_session_ids == [], (
        "Session whose short-id surface_ref is the prefix of a live "
        "daemon UUID must not be reaped as phantom"
    )


def test_compute_drift_spawn_grace_window_protects_fresh_sessions() -> None:
    """Sessions younger than SPAWN_GRACE_SECONDS are not reaped as phantom.

    Regression test for #271: ``claude --bg`` spawn → daemon roster
    registration is async. A reconcile call in the same dispatch tick as
    the spawn would otherwise see the not-yet-registered session as a
    phantom and reap it within 1 second. Real-world latency observed
    2026-05-26: 0.3-1.5s between spawn and roster registration.
    """
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    fresh = _mk_session("fresh", "fresh-ref", started_at=now - timedelta(seconds=2))
    old = _mk_session("old", "old-ref", started_at=now - timedelta(seconds=120))
    state = CwState(sessions=[fresh, old])

    # native_live is empty (both refs missing from daemon roster)
    report = compute_drift(state, set(), now=now)

    # Only the old one is reaped; the fresh one is in the grace window.
    assert report.phantom_session_ids == ["old"]


def test_compute_drift_grace_expires_after_spawn_grace_seconds() -> None:
    """A session just past SPAWN_GRACE_SECONDS is eligible for phantom-reaping."""
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    just_expired = _mk_session(
        "expired",
        "expired-ref",
        started_at=now - timedelta(seconds=SPAWN_GRACE_SECONDS + 1),
    )
    state = CwState(sessions=[just_expired])
    report = compute_drift(state, set(), now=now)
    assert report.phantom_session_ids == ["expired"]


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
    # Use a realistic 8-char short id matching cw's _is_native_surface_ref
    # contract (the daemon would return the full UUID; we'd slice to 8).
    short_id = "abcd1234"
    full_uuid = f"{short_id}-1111-2222-3333-444455556666"
    sess = _mk_session("alive", short_id)
    save_state(CwState(sessions=[sess]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": full_uuid}],
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
        state, now=now, config=OrchestratorConfig()
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
        state, now=now, config=OrchestratorConfig()
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
        state, now=now, config=OrchestratorConfig()
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
        state, now=now, config=OrchestratorConfig()
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
        state, now=now, config=OrchestratorConfig()
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
        state, now=now, config=OrchestratorConfig()
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

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    assert short_id in daemon.stop_calls


# ---------------------------------------------------------------------------
# Stale-worktree cleanup on timeout (GitHub issue #404): a timed-out session's
# task is reverted to PENDING, so its worktree must be removed or the retry
# would inherit this run's branch/commits.
# ---------------------------------------------------------------------------


def test_revert_stalled_cleans_up_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wall-clock timeout removes the stale worktree so re-dispatch is clean."""
    worktree = tmp_path / "wt-cleanup"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "clean-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/clean-1"
    state = CwState(sessions=[sess])
    save_state(state)

    removed: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    assert removed == [("client-a", "auto-dev/clean-1", True)]


def test_revert_stalled_worktree_cleanup_is_best_effort(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree-removal failure must not abort the timeout sweep (#404)."""
    worktree = tmp_path / "wt-boom"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("boom-1", worktree, started_at, surface_ref=None)
    sess.branch = "auto-dev/boom-1"
    state = CwState(sessions=[sess])
    save_state(state)

    def boom(client: ClientConfig, branch: str, *, force: bool = False) -> None:
        msg = "git worktree remove exploded"
        raise WorktreeError(msg)

    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr("cw.reconcile.remove_worktree", boom)

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_skips_cleanup_when_no_branch(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session with no branch attempts no worktree cleanup (#404)."""
    worktree = tmp_path / "wt-nobranch"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "nobranch-1", worktree, started_at, surface_ref=None
    )
    # branch left as the model default (None)
    state = CwState(sessions=[sess])
    save_state(state)

    calls: list[str] = []

    def record_get_client(name: str) -> ClientConfig:
        calls.append(name)
        return ClientConfig(name=name, workspace_path=tmp_path / "ws")

    monkeypatch.setattr("cw.reconcile.get_client", record_get_client)

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    assert calls == []


# ---------------------------------------------------------------------------
# Dirty-check guard on worktree cleanup (GitHub issue #425): force-remove must
# be skipped when the worktree has unsaved work; task parks as BLOCKED_ON_USER.
# ---------------------------------------------------------------------------


def test_revert_stalled_skips_removal_and_blocks_task_when_dirty(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed-out session with unpushed commits: worktree NOT removed.

    Task must move to BLOCKED_ON_USER, not PENDING (#425).
    """
    worktree = tmp_path / "wt-dirty"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "dirty-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/dirty-1"
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="dirty-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="dirty-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    removed: list[str] = []
    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile.remove_worktree",
        lambda _client, branch, *, _force=False: removed.append(branch),
    )
    # Simulate dirty worktree (has unsaved work)
    monkeypatch.setattr("cw.reconcile.worktree_has_unsaved_work", lambda _c, _b: True)

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    # Worktree must NOT have been removed
    assert removed == []
    # Task must be BLOCKED_ON_USER (not PENDING)
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "dirty-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


def test_revert_stalled_removes_when_clean(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed-out session with clean worktree: removal proceeds as before."""
    worktree = tmp_path / "wt-cleanX"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session(
        "cleanX-1", worktree, started_at, surface_ref=None
    )
    sess.branch = "auto-dev/cleanX-1"
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cleanX-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cleanX-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    removed: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )
    # Clean worktree
    monkeypatch.setattr("cw.reconcile.worktree_has_unsaved_work", lambda _c, _b: False)

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    # Removal proceeds with force=True
    assert removed == [("client-a", "auto-dev/cleanX-1", True)]
    # Task reverted to PENDING (normal timeout path)
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "cleanX-1")
    assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# Sentinel-salvage tests (GitHub issue #372): a stalled/crashed session that
# emitted a terminal-success sentinel must be dispositioned by that sentinel,
# not mislabeled timed_out/crash, and its ticket must NOT be re-dispatched.
# ---------------------------------------------------------------------------


def _shipped_salvage_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "salv-1",
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": 12,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
        "branch": "auto-dev/salv-1",
        "worktree_path": "/tmp/wt/salv-1",
        "fork_point_sha": "abc1234",
        "commits": ["sha1"],
        "pr": {
            "number": 99,
            "url": "https://github.com/foo/bar/pull/99",
            "auto_merge": True,
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
        "cost_usd": 1.5,
        "next_actions": ["wait_for_ci"],
    }


def _no_op_salvage_payload() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "ticket_id": "salv-noop",
        "status": "no_op",
        "stage_reached": "stage1_pre_flight",
        "scope": {
            "tier": "small",
            "files": 0,
            "lines_estimate": 0,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "none",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
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
        "next_actions": ["close_issue_as_completed"],
    }


def _write_salvage_transcript(
    home: Path, worktree: Path, claude_session_id: str, payload: dict[str, Any]
) -> Path:
    """Write a transcript jsonl under ``home`` carrying a wrapped sentinel.

    Mirrors Claude's on-disk layout: ``<home>/.claude/projects/<encoded>/
    <uuid>.jsonl`` with the encoded path replacing both ``/`` and ``.``
    with ``-`` (matching Claude Code's actual encoding).
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload)
    sentinel = f"narrative\n<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": sentinel}],
        },
    }
    path = project_dir / f"{claude_session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return path


def test_revert_stalled_salvages_shipped_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget session that shipped → COMPLETED, task NOT reverted."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-1", worktree, started_at)
    _write_salvage_transcript(
        home, worktree, "claude-uuid-1", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-1",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    # Not reverted for re-dispatch.
    assert reverted == []
    reloaded = next(s for s in load_state().sessions if s.id == "salv-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"
    assert reloaded.claude_session_id == "claude-uuid-1"
    assert reloaded.cost_usd == 1.5

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-1")
    assert task.status == QueueItemStatus.COMPLETED

    events = read_events(
        consumer="test-salv-1",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    payload = next(e.payload for e in events if e.payload.get("ticket_id") == "salv-1")
    assert payload["crashed"] is False
    assert payload["salvaged"] is True


def test_revert_stalled_salvages_no_op_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget session that no-op'd → COMPLETED, task NOT reverted."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-noop"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-noop", worktree, started_at)
    _write_salvage_transcript(home, worktree, "claude-uuid-2", _no_op_salvage_payload())
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-noop",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-noop",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    assert reverted == []
    reloaded = next(s for s in load_state().sessions if s.id == "salv-noop")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "no_op"
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-noop")
    assert task.status == QueueItemStatus.COMPLETED


def test_revert_stalled_no_salvage_without_sentinel_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh transcript but no terminal sentinel → TIMED_OUT + revert (unchanged)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-nosent"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-none", worktree, started_at)
    # Transcript exists but carries no AUTO_DEV_RESULT block.
    proj = claude_project_dir(worktree)
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "claude-uuid-3.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "still working on it"}],
                },
            }
        )
        + "\n"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-none",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-none",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    assert "salv-none" in reverted
    reloaded = next(s for s in load_state().sessions if s.id == "salv-none")
    assert reloaded.status == SessionStatus.TIMED_OUT
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-none")
    assert task.status == QueueItemStatus.PENDING


def test_revert_stalled_ignores_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript older than started_at (reused worktree, #358) → TIMED_OUT."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-stale"
    # started_at in the future relative to the (real-now) transcript mtime, so
    # the freshly-written transcript is "stale" by the started_at guard.
    started_at = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2099, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-stale", worktree, started_at)
    _write_salvage_transcript(
        home, worktree, "claude-uuid-4", _shipped_salvage_payload()
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-stale",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-stale",
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    # Stale transcript ignored → genuine timeout.
    assert "salv-stale" in reverted
    reloaded = next(s for s in load_state().sessions if s.id == "salv-stale")
    assert reloaded.status == SessionStatus.TIMED_OUT


def test_reconcile_crashed_phantom_salvages_shipped_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom (surface gone) session that shipped → COMPLETED, not re-dispatched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-crash"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under the headless budget, so the
    # timeout sweep ignores it and the crashed-phantom sweep handles it.
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-crash", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "salv-crash"
    _write_salvage_transcript(home, worktree, "claude-uuid-5", payload)
    # A second, genuinely-live session keeps the daemon roster non-empty so the
    # transient-outage guard does not trip (it would otherwise abort reconcile
    # when native_live is empty). Its ref IS in the live set, so it is not a
    # phantom; only "gone-ref" is.
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-crash",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-crash",
                )
            ]
        )
    )

    # Only "live-ref" is live → "gone-ref" is a phantom; non-empty roster
    # keeps the outage guard from tripping.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "salv-crash" not in report.reverted_ticket_ids
    reloaded = next(s for s in load_state().sessions if s.id == "salv-crash")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-crash")
    assert task.status == QueueItemStatus.COMPLETED


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


# ---------------------------------------------------------------------------
# resolve_headless_budget tests (GitHub issue #265)
# ---------------------------------------------------------------------------


def test_resolve_headless_budget_small_tier(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-tier default: session with scope.tier='small' → 1800s."""
    worktree = tmp_path / "wt-small"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="small-tier-sess",
        name="client-a/auto-dev/GEN-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "small"}},
    )
    config = OrchestratorConfig(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 1800


def test_resolve_headless_budget_large_tier(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-tier default: session with scope.tier='large' → 5400s."""
    worktree = tmp_path / "wt-large"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="large-tier-sess",
        name="client-a/auto-dev/GEN-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "large"}},
    )
    config = OrchestratorConfig(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 5400


def test_resolve_headless_budget_per_ticket_override(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-ticket override beats tier: headless_timeout_override=7200 > small=1800."""
    worktree = tmp_path / "wt-override"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="override-sess",
        name="client-a/auto-dev/GEN-3",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "small"}},
    )
    task = TicketTask(
        ticket_id="GEN-3",
        client="client-a",
        headless_timeout_override=7200,
    )
    config = OrchestratorConfig(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 7200


def test_resolve_headless_budget_pre_stage1_fallback(
    tmp_config_dir: Path,
) -> None:
    """Pre-Stage-1 fallback: no task, no last_result → HEADLESS_TIMEOUT_SECONDS."""
    sess = Session(
        id="fallback-sess",
        name="client-a/auto-dev/GEN-4",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result=None,
    )
    config = OrchestratorConfig(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == HEADLESS_TIMEOUT_SECONDS


def test_revert_stalled_uses_per_session_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with tier='small' (budget=1800) elapsed 2000s → timed out (< 3600)."""
    worktree = tmp_path / "wt-per-sess"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # 2000s elapsed: > 1800 (small tier) but < 3600 (global fallback)
    now = datetime(2026, 1, 1, 0, 33, 20, tzinfo=UTC)
    assert (now - started_at).total_seconds() == 2000

    sess = _mk_headless_daemon_session("per-sess-small", worktree, started_at)
    sess.last_result = {"scope": {"tier": "small"}}
    config = OrchestratorConfig(headless_timeout_by_tier={"small": 1800, "large": 5400})

    # Verify resolve_headless_budget returns 1800 for this session
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 1800

    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="per-sess-small",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="per-sess-small",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(state, now=now, config=config)

    assert "per-sess-small" in reverted
    assert sess.status == SessionStatus.TIMED_OUT


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
# flag_silently_idle_daemon_sessions tests (GitHub issue #129)
# ---------------------------------------------------------------------------


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

    with patch("cw.reconcile.fire_push_notification") as mock_notify:
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
        )
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "SILENT-1" in blocked
    # #348: flag-only — session stays ACTIVE so daemon worker can keep running.
    # Operators disposition flagged sessions via `cw spawn complete` /
    # `cw doctor --reap`. last_result.paused_status still set to prevent
    # double-firing via _has_terminal_sentinel on subsequent ticks (#324).
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
        patch("cw.reconcile._transcript_recently_active", return_value=True),
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
    ):
        result = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
    )

    assert blocked == []
    events = read_events(
        consumer="test-crash-recovery",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 0


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

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="idle-salv-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="idle-salv-1",
                    attempts=2,  # at cap — would park without salvage
                )
            ]
        )
    )

    mock_daemon = MagicMock()
    with (
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
        # Transcript mtime is real-time (May 2026) but now is fake (Jan 2026);
        # negative diff < window_seconds would falsely mark the worker alive.
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
    ):
        state = load_state()
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=OrchestratorConfig()
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
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
    ):
        state = load_state()
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"fake-short-id"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        state = load_state()
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": full_uuid}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "RCL-S" in report.reverted_ticket_ids

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "RCL-S")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


# resolve_idle_watchdog_budget + per-tier/per-ticket override tests (#326)
# ---------------------------------------------------------------------------


def test_resolve_idle_watchdog_budget_returns_global_default_with_no_task() -> None:
    """No task → global IDLE_WATCHDOG_SECONDS fallback."""
    config = OrchestratorConfig()
    assert resolve_idle_watchdog_budget(None, config) == IDLE_WATCHDOG_SECONDS


def test_resolve_idle_watchdog_budget_no_scope_hint_returns_global_default() -> None:
    """Task with no scope_hint → global fallback."""
    config = OrchestratorConfig()
    task = TicketTask(ticket_id="T-1", client="c", scope_hint=None)
    assert resolve_idle_watchdog_budget(task, config) == IDLE_WATCHDOG_SECONDS


def test_resolve_idle_watchdog_budget_respects_per_tier() -> None:
    """scope_hint='large' → idle_watchdog_by_tier['large'] returned."""
    config = OrchestratorConfig(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="large")
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_resolve_idle_watchdog_budget_global_config_override_no_task() -> None:
    """config.idle_watchdog_seconds overrides the hardcoded fallback (no task)."""
    config = OrchestratorConfig(idle_watchdog_seconds=1800)
    assert resolve_idle_watchdog_budget(None, config) == 1800


def test_resolve_idle_watchdog_budget_global_config_override_no_scope_hint() -> None:
    """A pre-Stage-1 task (no scope_hint) uses the global config override, not
    the hardcoded 900s — this is the fanout-cascade fix (workers reaped mid-work)."""
    config = OrchestratorConfig(idle_watchdog_seconds=1800)
    task = TicketTask(ticket_id="T-1", client="c", scope_hint=None)
    assert resolve_idle_watchdog_budget(task, config) == 1800


def test_resolve_idle_watchdog_budget_per_tier_beats_global_override() -> None:
    """A resolvable per-tier budget still wins over the global config default."""
    config = OrchestratorConfig(
        idle_watchdog_seconds=1800, idle_watchdog_by_tier={"large": 600}
    )
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="large")
    assert resolve_idle_watchdog_budget(task, config) == 600


def test_resolve_idle_watchdog_budget_per_ticket_overrides_tier() -> None:
    """idle_watchdog_override beats per-tier dict."""
    config = OrchestratorConfig(idle_watchdog_by_tier={"large": 600})
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

    config = OrchestratorConfig(idle_watchdog_by_tier={"large": 1800})
    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=config
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


def test_resolve_idle_watchdog_budget_unknown_tier_falls_back_to_global() -> None:
    """scope_hint not in idle_watchdog_by_tier → global IDLE_WATCHDOG_SECONDS."""
    config = OrchestratorConfig(idle_watchdog_by_tier={"large": 600})
    task = TicketTask(ticket_id="T-1", client="c", scope_hint="unknown")
    assert resolve_idle_watchdog_budget(task, config) == IDLE_WATCHDOG_SECONDS


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

    config = OrchestratorConfig()
    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=config
    )

    assert blocked == []
    assert sess.status == SessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# flag_silently_idle — transcript liveness check (GitHub #340)
# ---------------------------------------------------------------------------


def _write_idle_transcript(
    home: Path, worktree: Path, filename: str = "sess.jsonl"
) -> Path:
    """Write a minimal transcript .jsonl under the project dir for *worktree*."""
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    record = '{"type": "assistant", "message": {"role": "assistant", "content": []}}\n'
    path.write_text(record)
    return path


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

    transcript = _write_idle_transcript(home, worktree)
    # Stamp at half the liveness window — well within TRANSCRIPT_LIVENESS_WINDOW_SECONDS
    half_window = TRANSCRIPT_LIVENESS_WINDOW_SECONDS // 2
    recent_ts = (now - timedelta(seconds=half_window)).timestamp()
    os.utime(str(transcript), (recent_ts, recent_ts))

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    with patch("cw.reconcile.fire_push_notification"):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    blocked = flag_silently_idle_daemon_sessions(
        state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
    )

    assert blocked == []
    assert sess.last_result is None


# ---------------------------------------------------------------------------
# Helpers for _awaiting_subagent tests
# ---------------------------------------------------------------------------


def _make_daemon_session(
    *, claude_session_id: str | None = None, surface_ref: str = "live-ref"
) -> Session:
    return Session(
        id="sess-1",
        name="client-a/auto-dev/T-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=Path("/tmp/wt"),
        surface_ref=surface_ref,
        claude_session_id=claude_session_id,
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# _awaiting_subagent tests (Task A1)
# ---------------------------------------------------------------------------


def test_awaiting_subagent_true_when_tail_is_pending_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Last assistant turn is a tool_use with no tool_result yet → awaiting."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    tu_ts = "2026-01-01T00:04:00Z"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": tu_ts,
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_false_when_tool_result_delivered(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """tool_use followed by tool_result → NOT awaiting (genuine hang)."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    lines = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:04:00Z",
            "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Agent"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:04:01Z",
            "message": {"content": [{"type": "tool_result"}]},
        },
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_false_when_pending_tool_use_too_old(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use older than SUBAGENT_LIVENESS_WINDOW → hung subagent."""
    from cw.reconcile import SUBAGENT_LIVENESS_WINDOW_SECONDS, _awaiting_subagent

    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    assert SUBAGENT_LIVENESS_WINDOW_SECONDS < 1800
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:30:00Z",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


# ---------------------------------------------------------------------------
# Task A2: watchdog skips workers awaiting a subagent
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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=True),
        patch("cw.reconcile.fire_push_notification") as mock_notify,
    ):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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

    assert resolve_idle_retry_cap(None, OrchestratorConfig()) == DEFAULT_IDLE_RETRY_CAP


def test_resolve_idle_retry_cap_respects_per_tier() -> None:
    from cw.reconcile import resolve_idle_retry_cap

    cfg = OrchestratorConfig(idle_retry_cap_by_tier={"large": 4})
    task = TicketTask(ticket_id="T", client="c", scope_hint="large")
    assert resolve_idle_retry_cap(task, cfg) == 4


def test_resolve_idle_retry_cap_unknown_tier_falls_back() -> None:
    from cw.reconcile import DEFAULT_IDLE_RETRY_CAP, resolve_idle_retry_cap

    cfg = OrchestratorConfig(idle_retry_cap_by_tier={"large": 4})
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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile.fire_push_notification") as mock_notify,
    ):
        flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
        patch("cw.reconcile.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile.fire_push_notification"),
        patch(
            "cw.reconcile.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        ),
        patch(
            "cw.reconcile.remove_worktree",
            lambda client, branch, *, force=False: removed.append(
                (client.name, branch, force)
            ),
        ),
    ):
        flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
        patch("cw.reconcile.get_native_daemon_client", return_value=MagicMock()),
        patch("cw.reconcile.fire_push_notification"),
        patch("cw.reconcile.get_client", record_get_client),
    ):
        flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
        )

    assert calls == []
    assert sess.status == SessionStatus.TIMED_OUT


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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
        patch("cw.reconcile.fire_push_notification") as mock_notify,
    ):
        blocked = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
        )
        mock_daemon.stop.assert_not_called()
        mock_notify.assert_called_once_with(sess.name, sess.client)

    assert "HANG-2" in blocked
    assert sess.status == SessionStatus.ACTIVE
    assert sess.last_result == {"paused_status": "silently_idle"}
    store = load_dev_queue()
    assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# _awaiting_subagent edge-case coverage
# ---------------------------------------------------------------------------


def test_awaiting_subagent_skips_blank_lines_and_bad_json(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Blank lines and malformed JSON in transcript are skipped gracefully."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    # blank line, bad JSON, then valid tool_use with no result
    transcript.write_text(
        "\n"
        "not-json\n"
        + json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:04:00Z",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_skips_non_list_content(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Entries with non-list content field are skipped without error."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    # entry with non-list content — should be skipped, not crash
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:04:00Z",
                "message": {"content": "not-a-list"},
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        # Nothing to track — returns False (no pending tool_use)
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_handles_invalid_timestamp(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Invalid ISO timestamp on tool_use → last_tool_use_ts stays None → False."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-valid-timestamp",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        # invalid ts → last_tool_use_ts = None → returns False
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_returns_false_on_oserror(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """OSError while reading transcript → fail-open False."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # Point at a file that does not exist
    sess = _make_daemon_session(claude_session_id="no-such-file")
    # project_dir exists but the specific .jsonl doesn't
    with patch("cw.reconcile._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


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
        patch("cw.reconcile._transcript_recently_active", return_value=False),
        patch("cw.reconcile._awaiting_subagent", return_value=False),
        patch("cw.reconcile.get_native_daemon_client", return_value=mock_daemon),
    ):
        result = flag_silently_idle_daemon_sessions(
            state, now=now, native_live={"live-ref"}, config=OrchestratorConfig()
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


def test_reconcile_tolerates_malformed_json_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json.JSONDecodeError from _claude_agents_json → daemon_errored semantics.

    reconcile() must NOT raise; with live ACTIVE sessions present the outage
    guard fires and state is left unchanged (#432).
    """
    state = CwState(
        sessions=[
            _mk_session("s-json", "ref-json"),
        ]
    )
    save_state(state)

    def _bad_json() -> list[dict[str, object]]:
        msg = "bad json"
        raise json.JSONDecodeError(msg, "", 0)

    monkeypatch.setattr("cw.reconcile._claude_agents_json", _bad_json)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-json")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


def test_reconcile_tolerates_file_not_found_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileNotFoundError (claude not on PATH) → daemon_errored semantics.

    reconcile() must NOT raise; with live ACTIVE sessions present the outage
    guard fires and state is left unchanged (#432).
    """
    state = CwState(
        sessions=[
            _mk_session("s-fnf", "ref-fnf"),
        ]
    )
    save_state(state)

    def _no_binary() -> list[dict[str, object]]:
        msg = "No such file or directory: 'claude'"
        raise FileNotFoundError(msg)

    monkeypatch.setattr("cw.reconcile._claude_agents_json", _no_binary)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-fnf")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


def test_resolve_idle_watchdog_honors_zero(
    tmp_config_dir: Path,
) -> None:
    """idle_watchdog_seconds=0 is honored as 0, not silently replaced by 900.

    The `or` operator treats 0 as falsy and falls back to the constant.
    The fix uses an explicit None check so 0 passes through (#432).
    """
    config = OrchestratorConfig(idle_watchdog_seconds=0)
    budget = resolve_idle_watchdog_budget(task=None, config=config)
    assert budget == 0, (
        f"idle_watchdog_seconds=0 should be honoured as 0, got {budget} "
        f"(likely silently fell back to IDLE_WATCHDOG_SECONDS={IDLE_WATCHDOG_SECONDS})"
    )


# ---------------------------------------------------------------------------
# GitHub issue #431: salvage all terminal-no-retry statuses + skip parked sessions
# ---------------------------------------------------------------------------


def _make_terminal_payload(status: str, ticket_id: str) -> dict[str, Any]:
    """Build a minimal valid AutoDevResult payload for the given terminal status."""
    # Base shape shared by most statuses.
    base: dict[str, Any] = {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": status,
        "stage_reached": "stage1_plan",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": None,
            "forbidden_touched": False,
        },
        "plan_source": "generated",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
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
        "next_actions": [],
    }
    if status == "plan_pending_approval":
        base["next_actions"] = ["user_approve_plan"]
    elif status == "review_pending_approval":
        # review_pending has a branch + impl stage
        base["stage_reached"] = "stage3_review"
        base["scope"]["lines_actual"] = 8
        base["branch"] = f"dev/{ticket_id}"
        base["fork_point_sha"] = "abc123"
        base["commits"] = ["sha1"]
        base["next_actions"] = ["user_approve_review"]
    elif status == "merge_gate_blocked":
        # merge_gate_blocked requires small tier (already set), branch, impl stage
        base["stage_reached"] = "stage4a_merge_gate"
        base["scope"]["lines_actual"] = 8
        base["branch"] = f"dev/{ticket_id}"
        base["fork_point_sha"] = "abc123"
        base["commits"] = ["sha1"]
        base["next_actions"] = ["resolve_merge_gate"]
    elif status == "ambiguities_pending_resolution":
        base["ambiguities"] = [{"question": "Open or closed enum?"}]
        base["next_actions"] = ["user_resolve_ambiguities"]
    elif status == "premises_pending_verification":
        base["premises"] = [{"claim": "PR #42 codified a deliberate decision"}]
        base["next_actions"] = ["user_verify_premises"]
    return base


@pytest.mark.parametrize(
    "status",
    [
        "scope_exceeded",
        "forbidden_area",
        "plan_pending_approval",
        "review_pending_approval",
        "merge_gate_blocked",
        "blocked",
        "shipped",
        "no_op",
    ],
)
def test_salvage_all_terminal_statuses_from_phantom(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom session whose transcript emits each non-retry terminal status must be
    salvaged (COMPLETED), NOT reverted to PENDING (#431).

    Covers the bug where _SALVAGE_TERMINAL_STATUSES was {"shipped", "no_op"} and
    missed scope_exceeded, forbidden_area, plan_pending_approval,
    review_pending_approval, merge_gate_blocked, and the PAUSED_FOR_USER_INPUT
    statuses.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"431-{status}"
    worktree = tmp_path / f"wt-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under headless budget (phantom path).
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        ticket_id, worktree, started_at, surface_ref="gone-ref"
    )
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-{status}", payload)

    alive = _mk_session("alive-431", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert ticket_id not in report.reverted_ticket_ids, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged terminal)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.COMPLETED, (
        f"status={status!r}: queue task must be COMPLETED, not re-dispatched"
    )


@pytest.mark.parametrize(
    "status",
    [
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    ],
)
def test_salvage_paused_statuses_from_phantom_route_to_blocked_on_user(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom session whose transcript emits a paused status must be salvaged and
    the queue task must be set to BLOCKED_ON_USER (not COMPLETED), so downstream
    operators know the session requires human input before re-dispatch (#471).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"471p-{status}"
    worktree = tmp_path / f"wt-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under headless budget (phantom path).
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        ticket_id, worktree, started_at, surface_ref="gone-ref"
    )
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-{status}", payload)

    alive = _mk_session("alive-471", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert ticket_id not in report.reverted_ticket_ids, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged paused)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"status={status!r}: queue task must be BLOCKED_ON_USER for paused status"
    )


@pytest.mark.parametrize(
    "status",
    [
        "scope_exceeded",
        "forbidden_area",
        "plan_pending_approval",
        "review_pending_approval",
        "merge_gate_blocked",
        "blocked",
        "shipped",
        "no_op",
    ],
)
def test_salvage_all_terminal_statuses_from_stalled(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled (wall-clock expired) headless session with each non-retry terminal
    status in transcript must be salvaged, NOT reverted to PENDING (#431)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"431s-{status}"
    worktree = tmp_path / f"wts-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-s-{status}", payload)

    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    assert ticket_id not in reverted, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged terminal)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.COMPLETED, (
        f"status={status!r}: queue task must be COMPLETED, not re-dispatched"
    )


@pytest.mark.parametrize(
    "status",
    [
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    ],
)
def test_salvage_paused_statuses_from_stalled_route_to_blocked_on_user(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled headless session with a paused status in transcript must be salvaged
    and the queue task set to BLOCKED_ON_USER, not COMPLETED (#471)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"471s-{status}"
    worktree = tmp_path / f"wts-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-s-{status}", payload)

    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=OrchestratorConfig()
    )

    assert ticket_id not in reverted, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged paused)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"status={status!r}: queue task must be BLOCKED_ON_USER for paused status"
    )


def test_salvage_terminal_statuses_constant_is_single_source_of_truth() -> None:
    """Drift guard: _SALVAGE_TERMINAL_STATUSES in reconcile.py must equal
    SALVAGE_TERMINAL_STATUSES from auto_dev_result.py (#431).

    This test exists purely to catch future drift between the two references.
    The implementation makes _SALVAGE_TERMINAL_STATUSES an alias, but this
    assertion ensures no one accidentally re-inlines a narrower set.
    """
    from cw.auto_dev_result import SALVAGE_TERMINAL_STATUSES as _SHARED
    from cw.reconcile import _SALVAGE_TERMINAL_STATUSES as _RECONCILE

    assert _RECONCILE == _SHARED, (
        "_SALVAGE_TERMINAL_STATUSES in reconcile.py drifted from "
        "SALVAGE_TERMINAL_STATUSES in auto_dev_result.py. "
        f"reconcile has {_RECONCILE!r}, shared has {_SHARED!r}"
    )


def test_revert_stalled_skips_parked_silently_idle_session(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session parked by flag_silently_idle_daemon_sessions (last_result has
    paused_status=silently_idle) must NOT be reverted to PENDING by the
    wall-clock timeout sweep, even when past the headless budget (#431).
    """
    worktree = tmp_path / "wt-parked"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("parked-idle", worktree, started_at)
    # Simulate a session parked by flag_silently_idle_daemon_sessions.
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="parked-idle",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="parked-idle",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reverted = revert_stalled_headless_sessions(
        state=state, now=now, config=OrchestratorConfig()
    )

    assert reverted == [], "Parked (silently_idle) session must NOT be reverted"
    assert sess.status == SessionStatus.ACTIVE, (
        "Parked session status must remain ACTIVE (flag-only park)"
    )
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "parked-idle")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER, (
        "Queue task must remain BLOCKED_ON_USER, not re-dispatched to PENDING"
    )


# ---------------------------------------------------------------------------
# Dot-encoding regression tests (GitHub issue #463)
# Worktrees under ~/.cw/ contain a dot in the path segment; Claude Code
# encodes BOTH '/' and '.' as '-'.  The old single-replace produced a
# path mismatch that caused all transcript liveness checks to return False,
# letting the idle watchdog falsely reap actively-working sessions.
# ---------------------------------------------------------------------------


def test_claude_project_dir_encodes_dots_as_dashes() -> None:
    """claude_project_dir replaces both '/' and '.' with '-' (Issue #463).

    For a worktree at /home/u/.cw/wt/abc/auto-dev-1 the encoded segment
    must be '-home-u--cw-wt-abc-auto-dev-1' (double dash for '.cw').
    """
    from cw._util import claude_project_dir as _cpd

    result = _cpd("/home/u/.cw/wt/abc/auto-dev-1")
    assert result.name == "-home-u--cw-wt-abc-auto-dev-1", (
        f"Expected double-dash for .cw segment; got {result.name!r}"
    )


def test_claude_project_dir_matches_verified_real_path() -> None:
    """Exact encoding match for the worktree documented in GitHub issue #463.

    Real worktree: /home/matthew/.cw/wt/7dc983e2/auto-dev-463
    Wrong (single replace): -home-matthew-.cw-wt-7dc983e2-auto-dev-463
    Correct (double replace): -home-matthew--cw-wt-7dc983e2-auto-dev-463
    """
    from cw._util import claude_project_dir as _cpd

    result = _cpd("/home/matthew/.cw/wt/7dc983e2/auto-dev-463")
    assert result.name == "-home-matthew--cw-wt-7dc983e2-auto-dev-463"
    # Confirm the old single-replace encoding is different (would not exist)
    wrong = "/home/matthew/.cw/wt/7dc983e2/auto-dev-463".replace("/", "-")
    assert wrong != result.name


def test_transcript_recently_active_finds_dotted_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_transcript_recently_active returns True for a ~/.cw/-style worktree.

    Regression for Issue #463: the old single-replace produced a path that
    didn't exist on disk, so the guard always returned False and the watchdog
    would reap active sessions.

    The worktree path contains a dot segment ('dot-cw') to reproduce the
    encoding mismatch without depending on the real ~/.cw directory.
    """
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Worktree path with a dot-prefixed segment, mirroring ~/.cw/wt/...
    worktree = tmp_path / ".dot-cw" / "wt" / "abc123" / "auto-dev-1"
    worktree.mkdir(parents=True, exist_ok=True)

    # Build the project dir using the CORRECT double-replace encoding.
    project_dir = claude_project_dir(worktree)
    project_dir.mkdir(parents=True, exist_ok=True)

    # Write a transcript that is "recent" (mtime = now).
    session_uuid = "test-uuid-dotted"
    transcript = project_dir / f"{session_uuid}.jsonl"
    record = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "still working"}],
            },
        }
    )
    transcript.write_text(record + "\n")

    sess = Session(
        id="dotted-wt-sess",
        name="client-a/auto-dev/DOTTED-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=tmp_path / "ws"
        ).workspace_path,
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        claude_session_id=session_uuid,
    )

    now = datetime.now(tz=UTC)
    # With the correct encoding the transcript is found → recently active.
    assert _transcript_recently_active(sess, now, window_seconds=60), (
        "Expected transcript to be found for dotted worktree path; "
        f"project_dir={project_dir!r} exists={project_dir.is_dir()}"
    )


def test_awaiting_subagent_finds_dotted_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_awaiting_subagent returns True for a ~/.cw/-style worktree with pending
    tool_use.

    Regression for Issue #463: single-replace caused _awaiting_subagent to hit
    its early-exit (project_dir not found), returning False and letting the
    watchdog fire on a session mid-subagent.
    """
    from cw.reconcile import _awaiting_subagent

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / ".dot-cw" / "wt" / "def456" / "auto-dev-2"
    worktree.mkdir(parents=True, exist_ok=True)

    project_dir = claude_project_dir(worktree)
    project_dir.mkdir(parents=True, exist_ok=True)

    session_uuid = "test-uuid-subagent"
    transcript = project_dir / f"{session_uuid}.jsonl"

    # Transcript ends with a tool_use with no following tool_result → awaiting.
    tool_use_ts = datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC).isoformat()
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": tool_use_ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_01", "name": "Task", "input": {}}
                ],
            },
        }
    )
    transcript.write_text(record + "\n")

    sess = Session(
        id="dotted-subagent-sess",
        name="client-a/auto-dev/DOTTED-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=tmp_path / "ws"
        ).workspace_path,
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        claude_session_id=session_uuid,
    )

    # now is within SUBAGENT_LIVENESS_WINDOW_SECONDS of the tool_use timestamp
    now = datetime(2026, 1, 1, 0, 16, 0, tzinfo=UTC)
    assert _awaiting_subagent(sess, now), (
        "Expected _awaiting_subagent=True for dotted worktree path; "
        f"project_dir={project_dir!r} exists={project_dir.is_dir()}"
    )


# ---------------------------------------------------------------------------
# session.phantom_reverted event tests (GitHub issue #459)
# ---------------------------------------------------------------------------


def test_phantom_reverted_event_emitted_with_dirty_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON phantom revert emits session.phantom_reverted with worktree_dirty=True."""
    sess = _mk_session("phantom-dirty", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-PD"
    sess.branch = "auto-dev/TICK-PD"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-PD",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr("cw.reconcile.worktree_has_unsaved_work", lambda _c, _b: True)

    reconcile()

    events = read_events(
        consumer="test-phantom-dirty",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "phantom-dirty"
    assert p["ticket_id"] == "TICK-PD"
    assert p["client"] == "client-a"
    assert p["worktree_dirty"] is True
    assert "worktree_path" in p
    assert events[0].correlation_id == "TICK-PD"


def test_phantom_reverted_event_emitted_with_clean_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON phantom revert emits session.phantom_reverted with dirty=False."""
    sess = _mk_session("phantom-clean", "dead-ref-2")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-PC"
    sess.branch = "auto-dev/TICK-PC"
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-PC",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr("cw.reconcile.worktree_has_unsaved_work", lambda _c, _b: False)

    reconcile()

    events = read_events(
        consumer="test-phantom-clean",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "phantom-clean"
    assert p["ticket_id"] == "TICK-PC"
    assert p["client"] == "client-a"
    assert p["worktree_dirty"] is False
    assert "worktree_path" in p
    assert events[0].correlation_id == "TICK-PC"


def test_phantom_reverted_not_emitted_for_user_origin(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USER-origin phantom does NOT emit session.phantom_reverted."""
    sess = _mk_session("phantom-user", "dead-ref-3")
    # Leave origin as default (USER)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(DevQueueStore(tasks=[]))

    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )

    reconcile()

    events = read_events(
        consumer="test-phantom-user-origin",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 0


# ---------------------------------------------------------------------------
# session.salvage_skipped event tests (GitHub issue #459)
# ---------------------------------------------------------------------------


def test_salvage_skipped_emitted_for_park_marker(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Park-marker session emits session.salvage_skipped."""
    worktree = tmp_path / "wt-parked-sk"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salvage-skip-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="salvage-skip-1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="salvage-skip-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    revert_stalled_headless_sessions(state=state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-salvage-skipped",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "salvage-skip-1"
    assert p["reason"] == _SALVAGE_SKIP_REASON
    assert p["paused_status"] == _SILENTLY_IDLE_REASON
    assert events[0].correlation_id == "salvage-skip-1"


def test_salvage_skipped_not_emitted_for_terminal_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session with a real terminal sentinel does NOT emit session.salvage_skipped."""
    worktree = tmp_path / "wt-salvaged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salvage-real-1", worktree, started_at)
    # last_result is None → no park marker; salvage will find a sentinel
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="salvage-real-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="salvage-real-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    # Mock _salvage_terminal_result to return a real terminal result so the
    # session bypasses the salvage-skipped gate entirely.
    fake_result = MagicMock()
    fake_result.cost_usd = None
    fake_result.status = "shipped"
    monkeypatch.setattr(
        "cw.reconcile._salvage_terminal_result",
        lambda *_args, **_kwargs: (fake_result, "fake-claude-id"),
    )

    revert_stalled_headless_sessions(state=state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-no-salvage-skip",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 0


def test_compute_worktree_dirty_returns_false_when_get_client_raises(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compute_worktree_dirty returns False when get_client raises (fail-safe)."""
    from cw.reconcile import _compute_worktree_dirty

    monkeypatch.setattr(
        "cw.reconcile.get_client",
        lambda _name: (_ for _ in ()).throw(ValueError("no such client")),
    )
    assert _compute_worktree_dirty("missing-client", "some-branch") is False


def test_salvage_skipped_emitted_with_null_ticket_id(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Park-marked session without auto-dev/ prefix: salvage_skipped, ticket_id=None."""
    worktree = tmp_path / "wt-no-tid"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    # Session name without auto-dev/ prefix → ticket_id_for_session returns None.
    # _is_headless requires a cw-context.json with "headless": true.
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "no-tid-sess"}'
    )
    sess = Session(
        id="no-tid-sess",
        name="client-a/impl",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=tmp_path / "ws",
        worktree_path=worktree,
        started_at=started_at,
        last_result={"paused_status": _SILENTLY_IDLE_REASON},
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    revert_stalled_headless_sessions(state=state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-salvage-skip-null-tid",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["ticket_id"] is None
    assert p["reason"] == _SALVAGE_SKIP_REASON
    assert events[0].correlation_id is None


# ---------------------------------------------------------------------------
# complete_timed_out_merged_tasks tests (#471)
# ---------------------------------------------------------------------------


def _mk_timed_out_daemon_session(
    ticket_id: str,
    completed_at: datetime,
    branch: str | None = None,
) -> Session:
    """Build a TIMED_OUT DAEMON session for complete_timed_out_merged_tasks tests."""
    return Session(
        id=ticket_id,
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.TIMED_OUT,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=completed_at - timedelta(hours=1),
        completed_at=completed_at,
        completed_reason=CompletionReason.TIMED_OUT,
        branch=branch,
    )


def test_complete_timed_out_merged_tasks_pr_merged(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session within 7 days, PR merged → task set to COMPLETED."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    completed_at = now - timedelta(days=2)  # within 7-day lookback

    sess = _mk_timed_out_daemon_session(
        "471m-1", completed_at, branch="auto-dev/471m-1"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="471m-1",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                )
            ]
        )
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            returncode = 0
            stdout = json.dumps([{"state": "MERGED"}])

        return _Result()

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)

    with freezegun.freeze_time(now):
        completed = complete_timed_out_merged_tasks()

    assert "471m-1" in completed
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "471m-1")
    assert task.status == QueueItemStatus.COMPLETED


def test_complete_timed_out_merged_tasks_pr_not_merged(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session within 7 days, PR not merged → task stays PENDING."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    completed_at = now - timedelta(days=2)

    sess = _mk_timed_out_daemon_session(
        "471m-2", completed_at, branch="auto-dev/471m-2"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="471m-2",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                )
            ]
        )
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            returncode = 0
            stdout = json.dumps([{"state": "OPEN"}])

        return _Result()

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)

    with freezegun.freeze_time(now):
        completed = complete_timed_out_merged_tasks()

    assert completed == []
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "471m-2")
    assert task.status == QueueItemStatus.PENDING


def test_complete_timed_out_merged_tasks_gh_unavailable(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session, gh raises FileNotFoundError → no change to task."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    completed_at = now - timedelta(days=2)

    sess = _mk_timed_out_daemon_session(
        "471m-3", completed_at, branch="auto-dev/471m-3"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="471m-3",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                )
            ]
        )
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)

    with freezegun.freeze_time(now):
        completed = complete_timed_out_merged_tasks()

    assert completed == []
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "471m-3")
    assert task.status == QueueItemStatus.PENDING


def test_complete_timed_out_merged_tasks_outside_lookback(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT DAEMON session older than 7 days → no change (outside lookback)."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    completed_at = now - timedelta(days=8)  # outside 7-day lookback

    sess = _mk_timed_out_daemon_session(
        "471m-4", completed_at, branch="auto-dev/471m-4"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="471m-4",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                )
            ]
        )
    )

    called: list[bool] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        called.append(True)

        class _Result:
            returncode = 0
            stdout = json.dumps([{"state": "MERGED"}])

        return _Result()

    monkeypatch.setattr("cw.reconcile.subprocess.run", _fake_run)

    with freezegun.freeze_time(now):
        completed = complete_timed_out_merged_tasks()

    assert completed == []
    # gh should not have been called at all (skipped before branch lookup).
    assert called == []
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "471m-4")
    assert task.status == QueueItemStatus.PENDING
