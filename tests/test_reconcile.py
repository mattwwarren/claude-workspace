"""Unit tests for cw.reconcile."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import freezegun
import pytest

from cw._util import claude_project_dir
from cw.auto_dev_result import (
    BLOCKER_REASON_VALIDATION_FAILED,
    AutoDevResult,
    BlockedResult,
    Blocker,
    parse_stdout,
)
from cw.config import (
    load_state,
    orchestrator_config_file,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import WorktreeError
from cw.models import (
    CW_STATE_SCHEMA_VERSION,
    DEFAULT_LANE,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LocalLivenessHandle,
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
    _DIRTY_WORKTREE_REASON,
    _NEEDS_SALVAGE_REASON,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_SKIP_REASON,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    HEADLESS_TIMEOUT_SECONDS,
    IDLE_WATCHDOG_SECONDS,
    SPAWN_GRACE_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    SentinelRouteOutcome,
    _act_on_local_harvest_candidates,
    _apply_sentinel_to_task,
    _claude_agents_json,
    _detect_local_harvest_candidates,
    _has_terminal_sentinel,
    _verify_supervisor_session_id,
    complete_timed_out_merged_tasks,
    compute_drift,
    flag_silently_idle_daemon_sessions,
    reconcile,
    resolve_headless_budget,
    resolve_idle_watchdog_budget,
    revert_completed_silent_tasks,
    revert_stalled_headless_sessions,
    revert_timed_out_tasks,
    salvage_committed_no_pr_sessions,
)
from cw.reconcile._shared import _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
from cw.reconcile.gate_recipes import (
    RECIPE_AUTO_ADOPT_PLAN,
    RECIPE_AUTO_APPROVE_REVIEW,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _make_terminal_payload,
    _mk_daemon_completed_session,
    _mk_daemon_session_with_worktree,
    _mk_headless_daemon_session,
    _mk_live_idle_daemon_session,
    _mk_phantom_daemon_session,
    _mk_session,
    _mk_timed_out_daemon_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _state_queue_snapshot,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)
from tests.conftest import (
    CapturedEvent,
    _make_daemon_session,
    _write_idle_transcript,
    plan_body,
    stub_fetch_plan,
)
from tests.test_reconcile_gate_recipes import (
    _clean_result,
    _make_session,
    _plan_result,
    _write_acme_clients_yaml,
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

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
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

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
    result = _claude_agents_json()
    assert result == []


def test_claude_agents_json_passes_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json passes a timeout= kwarg to subprocess.run (#1230).

    An unbounded `claude agents --json` call under sessions_lock wedges the
    fleet on any CLI hang — the timeout is the guard against that.
    """
    captured_kwargs: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        captured_kwargs.update(kwargs)

        class _Result:
            stdout = "[]"
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
    _claude_agents_json()
    assert "timeout" in captured_kwargs
    assert captured_kwargs["timeout"] == 15


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
        "cw.reconcile.core._claude_agents_json",
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


def test_compute_drift_skips_orchestrate_purpose_session() -> None:
    """ORCHESTRATE sessions are excluded from phantom detection (R5 guard).

    ORCHESTRATE binding sessions have no live worker process by design,
    so they must never be classified as phantoms.
    """
    state = CwState(
        sessions=[
            _mk_session("orch1", "missing-ref", purpose=SessionPurpose.ORCHESTRATE),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_still_flags_impl_phantom_with_orchestrate_present() -> None:
    """ORCHESTRATE guard does not suppress IMPL phantom detection."""
    state = CwState(
        sessions=[
            _mk_session("orch1", "missing-ref", purpose=SessionPurpose.ORCHESTRATE),
            _mk_session("impl1", "also-missing"),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == ["impl1"]


def test_reconcile_marks_phantom_completed_crashed(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile flips phantom sessions to COMPLETED/CRASHED and persists."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    save_state(state)

    # Non-empty live set bypasses outage guard; "missing-ref" is still not live.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
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
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
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
        "cw.reconcile.core._claude_agents_json",
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
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    reconcile()

    queue = load_dev_queue()
    assert queue.tasks[0].status == QueueItemStatus.PENDING
    assert queue.tasks[0].session_id is None


def test_reconcile_usage_limited_true_from_phantom_path(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() report has usage_limited=True when a phantom DAEMON session's
    transcript contains a usage-limit message (#804, Fix 3)."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-phantom-ul"
    surface_ref = "dead-ul-r"  # 8-char short id that doesn't appear in live set

    sess = _mk_phantom_daemon_session(
        "phantom-ul-reconcile",
        started_at,
        surface_ref=surface_ref,
        worktree_path=worktree,
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="phantom-ul-reconcile",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="phantom-ul-reconcile",
                )
            ]
        )
    )

    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "You've hit your session limit · resets 3:40am (America/New_York)",
        filename=f"{surface_ref}-sess-804r.jsonl",
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    # Non-empty live set bypasses outage guard; surface_ref not present → phantom.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    report = reconcile()

    assert report.usage_limited is True
    assert "phantom-ul-reconcile" in report.reverted_ticket_ids


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
        "cw.reconcile.core._claude_agents_json",
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
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
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

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _boom)
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
        "cw.reconcile.core._claude_agents_json",
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
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    report = reconcile()

    assert "43" in report.reverted_ticket_ids

    store = load_dev_queue()
    task = next(t for t in store.tasks if t.ticket_id == "43")
    assert task.status == QueueItemStatus.PENDING
    assert task.session_id is None


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
# revert_stalled_headless_sessions tests (GitHub issue #185)
# ---------------------------------------------------------------------------


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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)

    sess = _mk_headless_daemon_session(
        "stop-me", worktree, started_at, surface_ref=short_id
    )
    state = CwState(sessions=[sess])
    save_state(state)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert short_id in daemon.stop_calls


def test_revert_stalled_headless_sessions_merged_pr_completes_not_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: stalled session whose PR is merged → COMPLETED, not TIMED_OUT."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("315-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-merged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-merged" not in reverted
    assert sess.status == SessionStatus.COMPLETED
    assert sess.completed_reason == CompletionReason.NORMAL
    assert sess.reap_reason == ReapReason.WALL_CLOCK_BUDGET

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "315-merged")
    assert task_after.status == QueueItemStatus.COMPLETED
    assert task_after.session_id is None

    completed_events = read_events(
        consumer="test-315-merged-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert any(e.payload.get("session_id") == sess.id for e in completed_events)

    timed_out_events = read_events(
        consumer="test-315-merged-no-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in timed_out_events)


def test_revert_stalled_headless_sessions_not_merged_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: stalled session whose PR is not merged → TIMED_OUT unchanged."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-notmerged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    sess = _mk_headless_daemon_session("315-notmerged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-notmerged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-notmerged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    branch_cwds: list[object] = []

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    def _capture_branch_exists(_branch: str, **kw: object) -> tuple[bool | None, bool]:
        branch_cwds.append(kw.get("cwd"))
        return True, True

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin", _capture_branch_exists
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-notmerged" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    # branch_exists_on_origin scoped to client-a's repo cwd (#1279).
    assert branch_cwds == [Path("/tmp/ws-staged")]

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "315-notmerged")
    assert task_after.status == QueueItemStatus.PENDING

    timed_out_events = read_events(
        consumer="test-315-notmerged-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert any(e.payload.get("session_id") == sess.id for e in timed_out_events)

    completed_events = read_events(
        consumer="test-315-notmerged-no-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in completed_events)


def test_revert_stalled_headless_sessions_dangling_client_skips_gh_blocks_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1279 R7: a REVERT_TASK candidate whose client is absent from a populated
    clients.yaml skips BOTH gh calls (pr_is_merged_for_ticket and
    branch_exists_on_origin) and routes the ticket to BLOCKED_ON_USER."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-1279-dangling"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    # clients.yaml populated with a DIFFERENT client than the session's
    # (client-a) → client-a is dangling/config-drifted.
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        "  client-b:\n"
        "    workspace_path: /tmp/ws-other\n"
        "    default_branch: main\n"
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None)

    sess = _mk_headless_daemon_session("1279-dangling", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="1279-dangling",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1279-dangling",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    merged_calls: list[str] = []
    branch_calls: list[str] = []

    def _capture_merged(tid: str, **_kw: object) -> tuple[bool | None, bool]:
        merged_calls.append(tid)
        return True, True

    def _capture_branch(branch: str, **_kw: object) -> tuple[bool | None, bool]:
        branch_calls.append(branch)
        return True, True

    monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture_merged)
    monkeypatch.setattr("cw.reconcile._deps.branch_exists_on_origin", _capture_branch)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Neither gh call fires for the dangling client (#1279 R7).
    assert merged_calls == []
    assert branch_calls == []

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "1279-dangling")
    assert task_after.status == QueueItemStatus.BLOCKED_ON_USER


def test_revert_stalled_headless_sessions_transient_gh_error_times_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315: transient gh error (None, True) → TIMED_OUT — fail-open behavior."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-315-gherror"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("315-gherror", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="315-gherror",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gherror",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "315-gherror" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-315-gherror-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert any(e.payload.get("session_id") == sess.id for e in timed_out_events)


def test_branch_absent_no_merged_pr_tags_session_timed_out_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (a): no merged PR + branch absent → TIMED_OUT, PENDING, branch_state."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-absent"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-absent", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-absent",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-absent",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (False, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Security assertion: branch-absent MUST NOT complete the session.
    assert "808-absent" in reverted
    assert sess.status == SessionStatus.TIMED_OUT
    assert sess.completed_reason == CompletionReason.TIMED_OUT

    store = load_dev_queue()
    task_after = next(t for t in store.tasks if t.ticket_id == "808-absent")
    assert task_after.status == QueueItemStatus.PENDING

    timed_out_events = read_events(
        consumer="test-808-absent-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert matching[0].payload.get("branch_state") == "absent_no_merged_pr"

    completed_events = read_events(
        consumer="test-808-absent-no-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == sess.id for e in completed_events)


def test_branch_present_omits_branch_state_key(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (b): no merged PR + branch present → TIMED_OUT, branch_state key absent."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-present"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-present", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-present",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-present",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "808-present" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-808-present-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert "branch_state" not in matching[0].payload


def test_branch_check_transient_error_omits_branch_state(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (c): branch-check error (None, True) → TIMED_OUT, branch_state omitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-transient"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-transient", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-transient",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-transient",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (None, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "808-transient" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    timed_out_events = read_events(
        consumer="test-808-transient-timed-out",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    matching = [e for e in timed_out_events if e.payload.get("session_id") == sess.id]
    assert len(matching) == 1
    assert "branch_state" not in matching[0].payload


def test_branch_check_not_called_when_pr_merged(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (d): merged PR → COMPLETED; branch_exists_on_origin never called."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-merged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    branch_check_count = 0

    def _branch_check_forbidden(_branch: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal branch_check_count
        branch_check_count += 1
        return (True, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        _branch_check_forbidden,
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert branch_check_count == 0
    assert sess.status == SessionStatus.COMPLETED


def test_branch_check_not_called_on_transient_pr_error(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#808 (e): transient PR error (None, True) → TIMED_OUT; branch check skipped."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-808-pr-transient"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("808-pr-transient", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="808-pr-transient",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="808-pr-transient",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, True),
    )

    branch_check_count = 0

    def _branch_check_forbidden(_branch: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal branch_check_count
        branch_check_count += 1
        return (True, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        _branch_check_forbidden,
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert branch_check_count == 0
    assert "808-pr-transient" in reverted
    assert sess.status == SessionStatus.TIMED_OUT


def test_revert_stalled_gh_prepass_skips_none_ticket_id_and_none_client(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315 pre-pass guard branches: ticket_id=None and client=None are skipped.

    Covers lines 512 (continue when ticket_id is None) and 514 (continue when
    client is None).  Neither candidate should reach pr_is_merged_for_ticket.

    - Session A: name "client-a/impl" → ticket_id_for_session returns None
      → candidate.ticket_id is None → line 512 continue.
    - Session B: normal auto-dev name with valid ticket, but _detect_stalled_candidates
      is patched to also emit a REVERT_TASK candidate with client=None to hit line 514.
    """
    from cw.reconcile import ProposedAction, ReapCandidate
    from cw.reconcile.stalled import _detect_stalled_candidates as _real_detect

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    # Session A — non-auto-dev name → ticket_id will be None in the real candidate.
    worktree_a = tmp_path / "wt-none-tid"
    worktree_a.mkdir(parents=True, exist_ok=True)
    (worktree_a / ".claude").mkdir(parents=True, exist_ok=True)
    (worktree_a / ".claude" / "cw-context.json").write_text(
        '{"headless": true, "session_id": "none-tid"}'
    )
    sess_a = Session(
        id="none-tid",
        name="client-a/impl",  # No "auto-dev/" prefix → ticket_id=None
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree_a,
        started_at=started_at,
    )
    state = CwState(sessions=[sess_a])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    # Wrap real detect to also inject a candidate with client=None (covers line 514).
    def _patched_detect(
        s: object,
        *,
        now: datetime,
        config: object,
        task_by_ticket: object,
    ) -> list[ReapCandidate]:
        real_candidates: list[ReapCandidate] = _real_detect(
            s,  # type: ignore[arg-type]
            now=now,
            config=config,  # type: ignore[arg-type]
            task_by_ticket=task_by_ticket,  # type: ignore[arg-type]
        )
        # Inject a REVERT_TASK candidate with a valid ticket_id but client=None.
        # Re-use sess_a's session_id so the act phase can resolve the session.
        # The candidate is skipped in the gh pre-pass (line 514: client is None),
        # then falls through to the normal TIMED_OUT revert path (no queue task
        # to update, so the queue remains unchanged).
        injected = ReapCandidate(
            session_id="none-tid",  # matches sess_a.id so act phase succeeds
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="TKT-NONE-CLIENT",
            client=None,  # Line 514: continue (skipped in gh pre-pass)
        )
        return [*real_candidates, injected]

    monkeypatch.setattr(
        "cw.reconcile.stalled._detect_stalled_candidates",
        _patched_detect,
    )

    # pr_is_merged_for_ticket must NOT be called for either skipped candidate.
    call_count = 0

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool, bool]:
        nonlocal call_count
        call_count += 1
        return (False, True)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        _should_not_be_called,
    )

    # Act — neither skipped candidate should trigger a gh call.
    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # Assert — gh was never called (both candidates were skipped before the call).
    assert call_count == 0


def test_revert_stalled_gh_prepass_second_candidate_skips_when_gh_gone(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#315 pre-pass: gh goes unavailable on first candidate → second short-circuits.

    Covers lines 523-525 (gh becomes unavailable for the first REVERT_TASK
    candidate: _merged, False return → set _gh_available=False, append to
    gh_blocked, continue) and lines 516-517 (second candidate sees
    _gh_available=False → append to gh_blocked, continue without calling gh).

    Both sessions end up in BLOCKED_ON_USER state via the gh_blocked routing.
    """
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    worktree_b = tmp_path / "wt-gh-gone-b"
    worktree_c = tmp_path / "wt-gh-gone-c"
    sess_b = _mk_headless_daemon_session("315-gh-gone-b", worktree_b, started_at)
    sess_c = _mk_headless_daemon_session("315-gh-gone-c", worktree_c, started_at)
    state = CwState(sessions=[sess_b, sess_c])
    save_state(state)

    task_b = TicketTask(
        ticket_id="315-gh-gone-b",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gh-gone-b",
    )
    task_c = TicketTask(
        ticket_id="315-gh-gone-c",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="315-gh-gone-c",
    )
    save_dev_queue(DevQueueStore(tasks=[task_b, task_c]))

    # First call → gh unavailable (None, False); second call must not happen.
    gh_call_count = 0

    def _first_call_unavailable(_tid: str, **_kw: object) -> tuple[None, bool]:
        nonlocal gh_call_count
        gh_call_count += 1
        return (None, False)  # _gh_avail=False → triggers lines 522-525

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        _first_call_unavailable,
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    # gh was called exactly once — second candidate hit the short-circuit (516-517).
    assert gh_call_count == 1
    # Neither session was reverted (both blocked on gh unavailability).
    assert "315-gh-gone-b" not in reverted
    assert "315-gh-gone-c" not in reverted
    # Both tasks remain BLOCKED_ON_USER (gh routing).
    store = load_dev_queue()
    task_b_after = next(t for t in store.tasks if t.ticket_id == "315-gh-gone-b")
    task_c_after = next(t for t in store.tasks if t.ticket_id == "315-gh-gone-c")
    assert task_b_after.status == QueueItemStatus.BLOCKED_ON_USER
    assert task_c_after.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# SESSION_STAGE_TIMED_OUT_RETRIED event (GitHub issue #724)
# ---------------------------------------------------------------------------


def test_session_stage_timed_out_retried_event_emitted_auto_policy(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto policy: genuine timeout emits SESSION_STAGE_TIMED_OUT_RETRIED."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-auto"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-auto", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-auto",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-auto",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-auto",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "retried-auto"
    assert payload["session_id"] == "retried-auto"
    assert payload["stage"] == Stage.PLAN
    assert payload["client"] == "client-a"
    assert payload["elapsed_seconds"] >= HEADLESS_TIMEOUT_SECONDS
    assert payload["attempts"] == 1


def test_session_stage_timed_out_retried_event_emitted_signal_only_policy(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal-only: SESSION_STAGE_TIMED_OUT_RETRIED fires before policy routing."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-signal"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-signal", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-signal",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-signal",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    # signal_only is the default OrchestratorConfig
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-retried-signal",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert len(events) == 1
    assert events[0].payload["ticket_id"] == "retried-signal"


def test_session_stage_timed_out_retried_not_emitted_for_merged_pr(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged PR: SESSION_STAGE_TIMED_OUT_RETRIED is NOT emitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-merged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-merged", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-merged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-merged",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (True, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-merged",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "retried-merged" for e in events)


def test_session_stage_timed_out_retried_not_emitted_for_gh_blocked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH-blocked candidate: SESSION_STAGE_TIMED_OUT_RETRIED is NOT emitted."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-gh-blocked"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("retried-gh-blocked", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="retried-gh-blocked",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="retried-gh-blocked",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (None, False),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-retried-gh-blocked",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "retried-gh-blocked" for e in events)


# ---------------------------------------------------------------------------
# SESSION_STAGE_TIMED_OUT_RETRIED edge-trigger — fires once, suppressed on
# re-detect (GitHub #782, Source A)
# ---------------------------------------------------------------------------


def test_timed_out_retried_not_reemitted_on_second_detect_signal_only(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal-only keeps session live → TIMED_OUT_RETRIED must fire exactly once."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-storm-a"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("storm-a", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="storm-a",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="storm-a",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    # Tick 1: event fires, reap_proposed_at stamped and persisted.
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    # Tick 2: reload from disk (mirrors _reconcile_locked production flow) to
    # verify suppression holds across the persistence boundary, not just in-memory.
    state = load_state()
    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-a",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    matching = [e for e in events if e.payload.get("ticket_id") == "storm-a"]
    assert len(matching) == 1, (
        f"Expected exactly 1 TIMED_OUT_RETRIED but got {len(matching)}"
    )


def test_timed_out_retried_suppressed_when_already_proposed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-stamped reap_proposed_at suppresses TIMED_OUT_RETRIED on that session."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-retried-storm-b"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("storm-b", worktree, started_at)
    # Pre-stamp reap_proposed_at to simulate a prior tick having already proposed.
    sess.reap_proposed_at = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="storm-b",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="storm-b",
        stage=Stage.PLAN,
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-b",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "storm-b" for e in events), (
        "TIMED_OUT_RETRIED must not fire when session already had reap_proposed_at"
    )


def test_timed_out_retried_fires_for_new_session_suppressed_for_existing(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed: new session fires TIMED_OUT_RETRIED; already-proposed suppressed."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree_new = tmp_path / "wt-storm-new"
    worktree_old = tmp_path / "wt-storm-old"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess_new = _mk_headless_daemon_session("storm-new", worktree_new, started_at)
    sess_old = _mk_headless_daemon_session("storm-old", worktree_old, started_at)
    sess_old.reap_proposed_at = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)

    state = CwState(sessions=[sess_new, sess_old])
    save_state(state)

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-new",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-new",
                    stage=Stage.PLAN,
                    attempts=1,
                ),
                TicketTask(
                    ticket_id="storm-old",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-old",
                    stage=Stage.PLAN,
                    attempts=1,
                ),
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=OrchestratorConfig())

    events = read_events(
        consumer="test-storm-mixed",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    fired_tids = {e.payload.get("ticket_id") for e in events}
    assert "storm-new" in fired_tids, "New session must fire TIMED_OUT_RETRIED"
    assert "storm-old" not in fired_tids, (
        "Already-proposed session must NOT fire TIMED_OUT_RETRIED"
    )


# ---------------------------------------------------------------------------
# resolve_stalled_retry_cap + stalled_retry_cap_by_tier config field (#756)
# ---------------------------------------------------------------------------


def test_resolve_stalled_retry_cap_default_with_no_task() -> None:
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, resolve_stalled_retry_cap

    assert resolve_stalled_retry_cap(None, _auto_config()) == DEFAULT_STALLED_RETRY_CAP


def test_resolve_stalled_retry_cap_respects_per_tier() -> None:
    from cw.reconcile import resolve_stalled_retry_cap

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 5})
    task = TicketTask(ticket_id="T", client="c", scope_hint="large")
    assert resolve_stalled_retry_cap(task, cfg) == 5


def test_resolve_stalled_retry_cap_unknown_tier_falls_back() -> None:
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, resolve_stalled_retry_cap

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 5})
    task = TicketTask(ticket_id="T", client="c", scope_hint="small")
    assert resolve_stalled_retry_cap(task, cfg) == DEFAULT_STALLED_RETRY_CAP


def test_stalled_retry_cap_reverts_below_cap(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempts < DEFAULT_STALLED_RETRY_CAP → normal REVERT_TASK path (PENDING)."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-below-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("below-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="below-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="below-cap",
        attempts=DEFAULT_STALLED_RETRY_CAP - 1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "below-cap" in reverted
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "below-cap")
    assert t.status == QueueItemStatus.PENDING

    # Regression guard for #724: SESSION_STAGE_TIMED_OUT_RETRIED must still fire
    # on the below-cap path (session is being retried, not parked).
    events = read_events(
        consumer="test-below-cap-retried",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert any(e.payload.get("ticket_id") == "below-cap" for e in events)


def test_stalled_retry_cap_parks_when_at_cap(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempts >= cap → BLOCKED_ON_USER, SESSION_NEEDS_ATTENTION emitted (#756)."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        HEADLESS_TIMEOUT_SECONDS,
    )

    worktree = tmp_path / "wt-at-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("at-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="at-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="at-cap",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
        lane="stalled-park-lane",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    reverted = revert_stalled_headless_sessions(
        state, now=now, config=_auto_config(headless_timeout_by_stage={})
    )

    assert "at-cap" not in reverted

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "at-cap")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    assert t.session_id is None
    assert t.disposition == _STALLED_CAP_PARKED_REASON

    s = next(s for s in state.sessions if s.id == "at-cap")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED

    events = read_events(
        consumer="test-at-cap",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "at-cap"
    assert payload["paused_status"] == _STALLED_CAP_PARKED_REASON
    assert payload["lane"] == "stalled-park-lane"


def test_stalled_retry_cap_no_retried_event_when_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parked by retry cap: SESSION_STAGE_TIMED_OUT_RETRIED must NOT fire (#756)."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-cap-no-retry-evt"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-no-evt", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-no-evt",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-no-evt",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-cap-no-evt",
        event_types=[OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED],
    )
    assert not any(e.payload.get("ticket_id") == "cap-no-evt" for e in events)


def test_stalled_revert_usage_limit_detected_sets_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: wall-clock REVERT_TASK path threads usage_limit_detected onto
    its ReapCandidate → reap_reason=usage_limit_cutoff when the session's
    transcript shows a usage-limit refusal."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-revert-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("revert-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="revert-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-ul",
        attempts=1,  # below cap -> normal REVERT_TASK path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    with patch("cw.reconcile._shared.detect_usage_limit", return_value=True):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

    assert "revert-ul" in reverted
    s = next(s for s in state.sessions if s.id == "revert-ul")
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_revert_no_usage_limit_keeps_wall_clock_budget_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: revert path's False arm — no usage-limit transcript keeps the
    original wall_clock_budget cause."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-revert-no-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("revert-no-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="revert-no-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-no-ul",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    with patch("cw.reconcile._shared.detect_usage_limit", return_value=False):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

    assert "revert-no-ul" in reverted
    s = next(s for s in state.sessions if s.id == "revert-no-ul")
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


def test_stalled_cap_park_usage_limit_detected_sets_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: the cap-park branch is NEW usage-limit logic (unlike the revert
    path, which mirrors idle.py precedent) — direct coverage required.
    attempts >= cap + usage-limit transcript →
    reap_reason=usage_limit_cutoff instead of stalled_retry_cap_parked."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        HEADLESS_TIMEOUT_SECONDS,
    )

    worktree = tmp_path / "wt-cap-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-ul",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    with patch("cw.reconcile._shared.detect_usage_limit", return_value=True):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config(headless_timeout_by_stage={})
        )

    assert "cap-ul" not in reverted  # still a park, not a requeue
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "cap-ul")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER
    # Task-level disposition is unaffected — only session.reap_reason changes.
    assert t.disposition == _STALLED_CAP_PARKED_REASON

    s = next(s for s in state.sessions if s.id == "cap-ul")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF


def test_stalled_cap_park_no_usage_limit_keeps_retry_cap_parked_cause(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030: cap-park branch's False arm — no usage-limit transcript keeps
    the original stalled_retry_cap_parked cause."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-cap-no-ul"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("cap-no-ul", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-no-ul",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-no-ul",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    with patch("cw.reconcile._shared.detect_usage_limit", return_value=False):
        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config(headless_timeout_by_stage={})
        )

    assert "cap-no-ul" not in reverted
    s = next(s for s in state.sessions if s.id == "cap-no-ul")
    assert s.status == SessionStatus.TIMED_OUT
    assert s.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED


def test_reconcile_usage_limited_true_from_stalled_wall_clock_path(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1030 root cause: reconcile()'s watchdog_usage_limited aggregation
    previously snapshotted pre_watchdog_timed_out_ids AFTER the stalled sweep
    already ran, so a usage-limit death caught by the stalled sweep's
    REVERT_TASK path (not just idle/phantom) could never reach
    ReconcileReport.usage_limited. This is the exact incident path: wall-clock
    budget expiry on a session that died to the account session limit."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-stalled-ul-reconcile"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    surface_ref = "stall-ul1"
    sess = _mk_headless_daemon_session(
        "stalled-ul-reconcile", worktree, started_at, surface_ref=surface_ref
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="stalled-ul-reconcile",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="stalled-ul-reconcile",
                    attempts=1,  # below cap -> normal REVERT_TASK path
                )
            ]
        )
    )

    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "You've hit your session limit · resets 11:50am ET",
        filename=f"{surface_ref}-sess-1030.jsonl",
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.branch_exists_on_origin",
        lambda _branch, **_kw: (True, True),
    )
    # Non-empty (decoy) live set bypasses the outage guard.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy00001111222233334444555566"}],
    )

    report = reconcile()

    assert report.usage_limited is True
    assert "stalled-ul-reconcile" in report.reverted_ticket_ids


def test_stalled_retry_cap_per_tier_override(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stalled_retry_cap_by_tier overrides default — large-tier cap=1 parks after 1."""
    from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

    worktree = tmp_path / "wt-tier-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("tier-cap", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="tier-cap",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="tier-cap",
        scope_hint="large",
        attempts=1,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )

    cfg = _auto_config(stalled_retry_cap_by_tier={"large": 1})
    reverted = revert_stalled_headless_sessions(state, now=now, config=cfg)

    assert "tier-cap" not in reverted
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "tier-cap")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


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
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", boom)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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

    monkeypatch.setattr("cw.reconcile._shared.get_client", record_get_client)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda _client, branch, *, _force=False: removed.append(branch),
    )
    # Simulate dirty worktree (has unsaved work)
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda client, branch, *, force=False: removed.append(
            (client.name, branch, force)
        ),
    )
    # Clean worktree
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

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
        state=load_state(), now=now, config=_auto_config()
    )

    # Not reverted for re-dispatch.
    assert reverted == []
    reloaded = next(s for s in load_state().sessions if s.id == "salv-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "shipped"
    assert reloaded.claude_session_id == "fake-short-id-claude-uuid-1"
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
        state=load_state(), now=now, config=_auto_config()
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
    """Stale transcript, no terminal sentinel → TIMED_OUT + revert (unchanged).

    The transcript's mtime is pinned stale (past the liveness floor) so the
    #1277 wall-clock liveness veto does not fire. Since #1283's widened lookup
    now also globs non-surface_ref-prefixed siblings, an un-pinned (real, fresh)
    mtime would otherwise be seen as live and veto the revert.
    """
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
    tx = proj / "claude-uuid-3.jsonl"
    tx.write_text(
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
    stale_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(tx, (stale_ts, stale_ts))
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
        state=load_state(), now=now, config=_auto_config()
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
        state=load_state(), now=now, config=_auto_config()
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
    _write_salvage_transcript(
        home, worktree, "claude-uuid-5", payload, surface_ref="gone-ref"
    )
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
        "cw.reconcile.core._claude_agents_json",
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


def test_reconcile_phantom_routes_stage_complete_advance_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom (surface gone) session that emitted stage_complete → advance, not revert.

    GitHub #716/#724: a staged worker finishes a stage, emits stage_complete,
    and exits (the staged engine spawns a fresh worker per stage). Because
    stage_complete is NOT in SALVAGE_TERMINAL_STATUSES, terminal salvage skipped
    it and the phantom path reverted RUNNING→PENDING with the stage UNCHANGED —
    so the next dispatch re-ran the SAME stage (the ~21-26 min/stage timeout tax).
    The fix routes the emitted advance sentinel through apply_staged_decision so
    the stage advances (IMPL→REVIEW) instead of being reverted as a crash.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-stage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under the headless budget, so the
    # timeout sweep ignores it and the crashed-phantom sweep handles it.
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-stage", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _stage_complete_payload()
    payload["ticket_id"] = "salv-stage"
    _write_salvage_transcript(
        home, worktree, "claude-uuid-stage", payload, surface_ref="gone-ref"
    )
    # Keep the daemon roster non-empty so the transient-outage guard does not trip.
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    # apply_staged_decision needs a pipeline to advance IMPL→REVIEW.
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-stage",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-stage",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    # NOT reverted (the bug re-ran the same stage); session completed normally.
    assert "salv-stage" not in report.reverted_ticket_ids
    reloaded = next(s for s in load_state().sessions if s.id == "salv-stage")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "stage_complete"
    # The decisive assertion: the stage ADVANCED IMPL→REVIEW (was stuck at IMPL).
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-stage")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING
    assert task.session_id is None


def test_reconcile_phantom_routes_tool_result_emitted_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom whose stage_complete sentinel was emitted via Bash tool_result.

    GitHub #731 (#722 dogfood): a worker that emits the sentinel with
    ``cat <<EOF`` lands the frame in a tool_result (stdout) block, not assistant
    text. The transcript scanners must find it there — otherwise neither the
    #716 phantom-advance nor the idle ROUTE_EMITTED_SENTINEL path can route it,
    and the stage stalls. Reproduces #722; fails before the #731 scan fix.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-toolres"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-toolres", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _stage_complete_payload()
    payload["ticket_id"] = "salv-toolres"
    _write_salvage_transcript(
        home,
        worktree,
        "claude-uuid-toolres",
        payload,
        surface_ref="gone-ref",
        emit_via="tool_result",
    )
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-toolres",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-toolres",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert "salv-toolres" not in report.reverted_ticket_ids
    reloaded = next(s for s in load_state().sessions if s.id == "salv-toolres")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-toolres")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING


def test_reconcile_phantom_non_advance_sentinel_not_routed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom with a non-advance sentinel (blocked) must NOT be routed as advance.

    Guards the #716 fix's status filter: only INTERMEDIATE_ADVANCE_STATUSES
    (stage_complete) advance the stage. A `blocked` sentinel is neither terminal
    salvage nor an advance, so it falls through to the crash path (BLOCKED_ON_USER
    under the default signal_only policy) — it must never silently advance.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-blocked"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-blocked", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _stage_complete_payload()
    payload["status"] = "blocked"
    payload["ticket_id"] = "salv-blocked"
    payload["blocker"] = {
        "stage": "stage2_impl",
        "reason": "impl_failed",
        "details": "tests failed twice",
    }
    _write_salvage_transcript(
        home, worktree, "claude-uuid-blocked", payload, surface_ref="gone-ref"
    )
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-blocked",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-blocked",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        reconcile()

    # Stage NOT advanced (the routed path's status filter rejected `blocked`).
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-blocked")
    assert task.stage == Stage.IMPL
    assert task.status == QueueItemStatus.BLOCKED_ON_USER


def test_reconcile_phantom_stage_mismatch_does_not_orphan_task_or_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale phantom-routed sentinel must not orphan the task or complete the
    session (GitHub #1019, the #986 incident reproduced via the phantom path).

    Same shape as ``test_reconcile_phantom_routes_stage_complete_advance_sentinel``
    (a phantom worker's transcript carries a stage_complete/stage2_impl advance
    sentinel), except the task's row has already advanced to REVIEW by the time
    the late/replayed sentinel is discovered. Pre-#1019 this would silently
    complete the session and leave the task's stage untouched but the session
    torn down -- a task with no owning live session and no route recorded. The
    guard must refuse: task stays exactly as it was, session is NOT completed,
    and SENTINEL_STAGE_MISMATCH is emitted.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-mismatch"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-mismatch", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "salv-mismatch"
    _write_salvage_transcript(
        home, worktree, "claude-uuid-mismatch", payload, surface_ref="gone-ref"
    )
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-mismatch",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-mismatch",
                    # Row already advanced past IMPL by the time this stale
                    # IMPL-leg sentinel is discovered -- the #986 shape.
                    stage=Stage.REVIEW,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        reconcile()

    # Task untouched: still RUNNING at REVIEW, no disposition stamped.
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-mismatch")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.RUNNING
    assert task.disposition is None

    # Session NOT completed/torn down -- the refusal must not orphan it.
    reloaded = next(s for s in load_state().sessions if s.id == "salv-mismatch")
    assert reloaded.status != SessionStatus.COMPLETED

    mismatch_events = read_events(
        consumer="test-salv-mismatch-sentinel-stage-mismatch",
        event_types=[OrchestratorEventType.SENTINEL_STAGE_MISMATCH],
    )
    assert any(e.payload.get("ticket_id") == "salv-mismatch" for e in mismatch_events)


def test_reconcile_phantom_race_already_failed_task_does_not_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1189: a task raced to FAILED by a concurrent caller must not be
    completed by the phantom sweep's ROUTE_EMITTED_SENTINEL path.

    Same shape as ``test_reconcile_phantom_stage_mismatch_does_not_orphan_
    task_or_complete_session`` (a phantom worker's transcript carries a
    stage_complete/stage2_impl advance sentinel), except the dev-queue task
    has already been landed FAILED/abandoned for this same ticket/session by
    the time the phantom sweep's own lookup runs (the R3(a) lookup-miss
    race) -- not a literal stage mismatch. Unlike the stage-mismatch sibling,
    no SENTINEL_STAGE_MISMATCH event fires: the race-miss return happens
    inside _apply_sentinel_to_task's lookup loop, before any delegation to
    dispatch.py's apply_staged_decision/_route_staged_decision, which is the
    only emitter of that event type.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-race"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        "salv-race", worktree, started_at, surface_ref="gone-ref"
    )
    payload = _stage_complete_payload()
    payload["ticket_id"] = "salv-race"
    _write_salvage_transcript(
        home, worktree, "claude-uuid-race", payload, surface_ref="gone-ref"
    )
    alive = _mk_session("alive", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="salv-race",
                    client="client-a",
                    # Already raced to terminal FAILED by a concurrent caller
                    # before this sweep's own lookup runs.
                    status=QueueItemStatus.FAILED,
                    session_id="salv-race",
                    stage=Stage.IMPL,
                    disposition="abandoned",
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        reconcile()

    # Task untouched: still FAILED/abandoned, nothing routed.
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-race")
    assert task.status == QueueItemStatus.FAILED
    assert task.disposition == "abandoned"

    # Session NOT completed/torn down -- the race-miss must not orphan it.
    reloaded = next(s for s in load_state().sessions if s.id == "salv-race")
    assert reloaded.status != SessionStatus.COMPLETED

    mismatch_events = read_events(
        consumer="test-salv-race-sentinel-stage-mismatch",
        event_types=[OrchestratorEventType.SENTINEL_STAGE_MISMATCH],
    )
    assert mismatch_events == []


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
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
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
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
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
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
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
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
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
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_scope_hint_large_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint='large' + session=None → large-tier budget."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    budget = resolve_headless_budget(task, None, config)
    assert budget == 5400
    assert budget != HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_scope_hint_small_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint='small' + session=None → small-tier budget."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="small")
    budget = resolve_headless_budget(task, None, config)
    assert budget == 1800


def test_resolve_headless_budget_no_scope_hint_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint=None + session=None → global timeout."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a")
    budget = resolve_headless_budget(task, None, config)
    assert budget == HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_override_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Step 1 (override) beats step 2.5 (scope_hint): override=9999 > large=5400."""
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    task = TicketTask(
        ticket_id="GEN-314",
        client="client-a",
        scope_hint="large",
        headless_timeout_override=9999,
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 9999


def test_resolve_headless_budget_last_result_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Step 2 (last_result tier) beats step 2.5 (scope_hint) when tier is present."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    sess = Session(
        name="client-a/auto-dev/GEN-314",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "small"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 1800  # small from last_result, not large from scope_hint


def test_resolve_headless_budget_non_dict_last_result_falls_to_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Non-dict last_result → AttributeError caught → step 2.5 scope_hint fires."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    sess = Session(
        name="client-a/auto-dev/GEN-314",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
    )
    sess.last_result = ["not", "a", "dict"]  # type: ignore[assignment]
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 5400  # scope_hint fires after AttributeError caught


def test_resolve_headless_budget_per_stage_hit_beats_tier(
    tmp_config_dir: Path,
) -> None:
    """Per-stage REVIEW default (7200) beats the small-tier default (1800)."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.REVIEW: 7200},
    )
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.REVIEW)
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "small"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 7200


def test_resolve_headless_budget_per_stage_hit_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Per-stage PLAN default (3600) beats the large-tier scope_hint (5400)."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.PLAN,
        scope_hint="large",
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 3600


def test_resolve_headless_budget_per_stage_hit_beats_global(
    tmp_config_dir: Path,
) -> None:
    """Per-stage IMPL default (4200) beats the global HEADLESS_TIMEOUT_SECONDS."""
    config = _auto_config(headless_timeout_by_stage={Stage.IMPL: 4200})
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.IMPL)
    budget = resolve_headless_budget(task, None, config)
    assert budget == 4200


def test_resolve_headless_budget_per_stage_override_still_beats_stage(
    tmp_config_dir: Path,
) -> None:
    """Step 1 (headless_timeout_override) still outranks step 1.5 (per-stage)."""
    config = _auto_config(headless_timeout_by_stage={Stage.REVIEW: 7200})
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.REVIEW,
        headless_timeout_override=9999,
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 9999


def test_resolve_headless_budget_stage_absent_falls_through_to_tier(
    tmp_config_dir: Path,
) -> None:
    """Stage absent from the per-stage map (HARDEN) falls through to tier."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.HARDEN)
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "large"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 5400


def test_resolve_headless_budget_stage_absent_falls_through_to_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Stage absent from the per-stage map (HARDEN) falls through to scope_hint."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.HARDEN,
        scope_hint="small",
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 1800


def test_resolve_headless_budget_task_none_skips_stage_step(
    tmp_config_dir: Path,
) -> None:
    """task=None short-circuits step 1.5 exactly as it already short-circuits step 1."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "large"}},
    )
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 5400


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
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )

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


def test_has_terminal_sentinel_unit(
    tmp_config_dir: Path,
) -> None:
    """_has_terminal_sentinel returns True only for dicts with a 'status' key."""
    base = Session(
        id="unit-sentinel",
        name="client-a/unit-sentinel",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    base.last_result = {"status": "shipped"}
    assert _has_terminal_sentinel(base) is True

    base.last_result = {"status": "blocked", "blocker": {"reason": "impl_failed"}}
    assert _has_terminal_sentinel(base) is True

    base.last_result = {"paused_status": "silently_idle"}
    assert _has_terminal_sentinel(base) is False

    base.last_result = {"paused_status": "needs_salvage"}
    assert _has_terminal_sentinel(base) is False

    base.last_result = None
    assert _has_terminal_sentinel(base) is False

    base.last_result = "not-a-dict"  # type: ignore[assignment]
    assert _has_terminal_sentinel(base) is False


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
    with patch("cw.reconcile.idle._transcript_recently_active", return_value=True):
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
    with patch("cw.reconcile.idle._transcript_recently_active", return_value=False):
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
        patch("cw.reconcile.idle._transcript_recently_active", return_value=False),
        patch("cw.reconcile.idle._awaiting_subagent", return_value=False),
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
        "cw.reconcile.idle._transcript_recently_active", lambda *_a, **_kw: False
    )
    monkeypatch.setattr(
        "cw.reconcile.idle._awaiting_subagent", lambda *_a, **_kw: False
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
    import json

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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_false_when_pending_tool_use_too_old(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use older than SUBAGENT_LIVENESS_WINDOW → hung subagent."""
    from cw.reconcile import SUBAGENT_LIVENESS_WINDOW_SECONDS, _awaiting_subagent

    # Window is 1800 s (30 min) as of #544; tool_use exactly at the boundary
    # (1800 s old) is expired (< is strict).
    assert SUBAGENT_LIVENESS_WINDOW_SECONDS == 1800
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:30:00Z",  # exactly 1800 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_true_for_20_min_old_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use ~20 min old is within the 1800 s window → alive (#544).

    A large refactor running a single tool call for 20-30 min must NOT be reaped.
    The liveness window was raised from 900→1800 s specifically to cover this case.
    """
    from cw.reconcile import _awaiting_subagent

    # now=01:00:00, tool_use at 00:40:00 → 20 min = 1200 s < 1800 → alive
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:40:00Z",  # 20 min = 1200 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_false_for_35_min_old_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use ~35 min old is beyond the 1800 s window → reaped (#544).

    The guard must not become permanent: a tool_use older than 1800 s indicates
    a genuinely hung subagent and the watchdog should still reap it.
    """
    from cw.reconcile import _awaiting_subagent

    # now=01:00:00, tool_use at 00:25:00 → 35 min = 2100 s > 1800 → expired
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:25:00Z",  # 35 min = 2100 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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


def test_detect_usage_limit_returns_true_when_message_present(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns True for a usage-limit phrase (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-ul-direct"
    sess = _mk_headless_daemon_session("ul-direct", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "You've hit your session limit · resets 5:20pm"
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    assert _detect_usage_limit(sess) is True


def test_detect_usage_limit_returns_false_when_absent(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False for a normal assistant message (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-no-ul"
    sess = _mk_headless_daemon_session("no-ul", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "Here is my analysis of the task."
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    assert _detect_usage_limit(sess) is False


def test_detect_usage_limit_returns_false_for_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False when transcript predates started_at (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)  # session started at 01:00
    worktree = tmp_path / "wt-stale-ul"
    sess = _mk_headless_daemon_session("stale-ul", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "You've hit your session limit · resets 5:20pm"
    )
    # Stamp BEFORE started_at so the stale-transcript guard fires.
    before_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (before_ts, before_ts))

    assert _detect_usage_limit(sess) is False


def test_detect_usage_limit_returns_false_when_no_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False when no transcript exists (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-no-trans"
    sess = _mk_headless_daemon_session("no-trans", worktree, started_at)
    # Do NOT write any transcript — project dir doesn't exist either.

    assert _detect_usage_limit(sess) is False


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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
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

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _bad_json)

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

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _no_binary)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-fnf")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


def test_reconcile_tolerates_timeout_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.TimeoutExpired from _claude_agents_json → daemon_errored semantics.

    A `claude agents --json` hang must not wedge sessions_lock or trigger
    mass-reaping. reconcile() must NOT raise; with live ACTIVE sessions
    present the outage guard fires and state is left unchanged (#1230).
    """
    state = CwState(
        sessions=[
            _mk_session("s-timeout", "ref-timeout"),
        ]
    )
    save_state(state)

    def _hangs() -> list[dict[str, object]]:
        raise subprocess.TimeoutExpired(cmd=["claude", "agents", "--json"], timeout=15)

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _hangs)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped, no reap/park mutation
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-timeout")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


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


# ---------------------------------------------------------------------------
# GitHub issue #431: salvage all terminal-no-retry statuses + skip parked sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "scope_exceeded",
        "forbidden_area",
        "merge_gate_blocked",
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
    _write_salvage_transcript(
        home, worktree, f"uuid-{status}", payload, surface_ref="gone-ref"
    )

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
        "cw.reconcile.core._claude_agents_json",
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
        "plan_pending_approval",
        "review_pending_approval",
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
    _write_salvage_transcript(
        home, worktree, f"uuid-{status}", payload, surface_ref="gone-ref"
    )

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
        "cw.reconcile.core._claude_agents_json",
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
        "merge_gate_blocked",
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
        state=load_state(), now=now, config=_auto_config()
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
        "plan_pending_approval",
        "review_pending_approval",
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
        state=load_state(), now=now, config=_auto_config()
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
        state=state, now=now, config=_auto_config()
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
# GitHub issue #403: backfill claude_session_id from daemon roster
# Workers parked before their first Stop hook never have claude_session_id
# written by spawn.  _transcript_recently_active and _awaiting_subagent
# degrade to scan-all without it.  Backfill during reconcile from the same
# _claude_agents_json() call that builds native_live.
# ---------------------------------------------------------------------------


# Frozen time for backfill tests: session started_at is 60s before frozen now
# (well within HEADLESS_TIMEOUT_SECONDS=3600) so revert_stalled does not fire.
_BACKFILL_FROZEN_NOW = "2026-06-01T12:00:00+00:00"
_BACKFILL_STARTED_AT = datetime(2026, 6, 1, 11, 59, 0, tzinfo=UTC)


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_happy_path(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE DAEMON session matches roster → claude_session_id backfilled."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-backfill"
    sess = _mk_headless_daemon_session(
        "backfill-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    assert sess.claude_session_id is None
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    # Reload from disk to verify persistence
    reloaded = next(s for s in load_state().sessions if s.id == "backfill-1")
    assert reloaded.claude_session_id == full_uuid


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_no_overwrite(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id already set → roster entry does not change it."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    existing = "existing-uuid"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-no-overwrite"
    sess = _mk_headless_daemon_session(
        "no-overwrite-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    sess.claude_session_id = existing
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "no-overwrite-1")
    assert reloaded.claude_session_id == existing


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_not_in_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from roster → claude_session_id stays None."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    worktree = tmp_path / "wt-not-in-roster"
    sess = _mk_headless_daemon_session(
        "not-in-roster-1", worktree, _BACKFILL_STARTED_AT, surface_ref="deadbeef"
    )
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "not-in-roster-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_outage(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roster fetch raises → no backfill, no crash; outage guard holds."""
    import subprocess as _subprocess

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-outage"
    sess = _mk_headless_daemon_session(
        "outage-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))

    def _raise(*args: object, **kwargs: object) -> None:
        raise _subprocess.CalledProcessError(1, "claude")

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _raise)
    # Must not raise
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "outage-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_same_tick_liveness(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After backfill, _transcript_recently_active uses the by-id path."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-liveness"
    sess = _mk_headless_daemon_session(
        "liveness-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    assert sess.claude_session_id is None
    save_state(CwState(sessions=[sess]))

    # Write the transcript under the full UUID filename (by-id path)
    _write_idle_transcript(home, worktree, filename=f"{full_uuid}.jsonl")

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()

    # Session should NOT be flagged silently idle — transcript found via by-id path
    reloaded = next(s for s in load_state().sessions if s.id == "liveness-1")
    assert reloaded.claude_session_id == full_uuid
    # Session still ACTIVE — watchdog did not fire
    assert reloaded.status == SessionStatus.ACTIVE


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_guard_branches(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard branches: surface_ref=None and non-DAEMON sessions are not backfilled."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-guard"

    # DAEMON ACTIVE but surface_ref=None
    daemon_no_ref = _mk_headless_daemon_session(
        "guard-no-ref", worktree, _BACKFILL_STARTED_AT, surface_ref=None
    )
    assert daemon_no_ref.claude_session_id is None

    # USER ACTIVE with matching surface_ref — should NOT be backfilled
    user_sess = _mk_session("guard-user", short_id, status=SessionStatus.ACTIVE)
    # _mk_session defaults to USER origin
    assert user_sess.origin is SessionOrigin.USER

    save_state(CwState(sessions=[daemon_no_ref, user_sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()

    state = load_state()
    reloaded_no_ref = next(s for s in state.sessions if s.id == "guard-no-ref")
    reloaded_user = next(s for s in state.sessions if s.id == "guard-user")
    assert reloaded_no_ref.claude_session_id is None
    assert reloaded_user.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_malformed_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-str sessionId in roster is skipped; valid entry still backfills."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-malformed"
    sess = _mk_headless_daemon_session(
        "malformed-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Roster has a malformed entry alongside the valid one
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": 12345}, {"sessionId": full_uuid}],
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "malformed-1")
    assert reloaded.claude_session_id == full_uuid


# ---------------------------------------------------------------------------
# Transcript-filename fallback tests (GitHub issue #522)
# When cw dev-queue wait drives a long run with no reconcile ticks, the
# session exits claude agents before backfill fires.  The transcript file
# persists, so _csid_from_transcript provides a last-resort resolution.
# ---------------------------------------------------------------------------


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_transcript_fallback(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from agents map but transcript exists.

    Resolved via transcript fallback.
    """
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]  # "04bf1c48"
    transcript_stem = f"{short_id}-{full_uuid}"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-tx-fallback"
    sess = _mk_headless_daemon_session(
        "tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Agents returns a non-matching session (daemon reachable, but our session exited)
    # An empty list would trigger the outage guard and abort reconcile entirely.
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    # Write transcript named <short_id>-<uuid>.jsonl
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    # Mtime must be after started_at
    after_started = _BACKFILL_STARTED_AT.timestamp() + 1
    os.utime(tx_path, (after_started, after_started))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "tx-fallback-1")
    assert reloaded.claude_session_id == transcript_stem


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_agents_primary_over_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents map takes priority over transcript when both are present."""
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    transcript_stem = f"{short_id}-different-uuid-xxxx"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-agents-primary"
    sess = _mk_headless_daemon_session(
        "agents-primary-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Agents map has the real uuid
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    # Transcript with a different stem — should NOT win
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    after_started = _BACKFILL_STARTED_AT.timestamp() + 1
    os.utime(tx_path, (after_started, after_started))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "agents-primary-1")
    # Agents map value wins
    assert reloaded.claude_session_id == full_uuid


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_no_transcript_fallback(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from agents map and no transcript → csid stays None."""
    short_id = "04bf1c48"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-no-tx"
    sess = _mk_headless_daemon_session(
        "no-tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Non-matching session so outage guard doesn't fire
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    # Manually create the project dir so it exists but is empty
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "no-tx-fallback-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript mtime <= started_at (stale reused-worktree) → csid stays None."""
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    transcript_stem = f"{short_id}-{full_uuid}"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-stale-tx"
    sess = _mk_headless_daemon_session(
        "stale-tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Non-matching session so outage guard doesn't fire
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    # Mtime exactly == started_at (not strictly after) — stale guard fires
    stale_mtime = _BACKFILL_STARTED_AT.timestamp()
    os.utime(tx_path, (stale_mtime, stale_mtime))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "stale-tx-fallback-1")
    assert reloaded.claude_session_id is None


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
    # Content-bearing timestamp (#1076): liveness now prefers this over mtime.
    now_ish = datetime.now(tz=UTC).isoformat()
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": now_ish,
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
# GitHub #1076: content-timestamp liveness (metadata-only transcript writes)
# ---------------------------------------------------------------------------


def _mk_content_ts_session(
    sid: str, worktree: Path, started_at: datetime, surface_ref: str = "fake-short-id"
) -> Session:
    """Build a DAEMON ACTIVE session for the #1076 content-timestamp tests."""
    return Session(
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


def test_transcript_recently_active_ignores_trailing_metadata_write(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing metadata-only write must not falsely resurrect liveness (#1076).

    Claude Code appends non-conversational record types (``ai-title`` etc.)
    that bump the transcript's mtime without representing genuine activity.
    Liveness must be derived from the last content-bearing entry's timestamp,
    not from mtime — otherwise a stale session's counter falsely resets.
    """
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-trailing-meta"
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
            # Metadata-only record: no "message" field, bumps mtime on write.
            {"type": "ai-title", "title": "Fix the thing"},
        ],
    )

    sess = _mk_content_ts_session("trailing-meta-1", worktree, started_at)

    assert _transcript_recently_active(sess, now, window_seconds=60) is False

    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - timedelta(hours=3).total_seconds()) < 5


def test_transcript_age_seconds_falls_back_to_mtime_without_content_timestamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No parseable content timestamp anywhere → fall back to mtime (#1076).

    Reuses ``_write_idle_transcript`` unmodified: its single record is
    content-bearing (assistant + dict message) but carries no "timestamp"
    key, exactly the "no content signal available" fallback case.
    """
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-fallback-mtime"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    _write_idle_transcript(home, worktree)

    sess = _mk_content_ts_session("fallback-mtime-1", worktree, started_at)

    now = datetime.now(tz=UTC)
    assert _transcript_recently_active(sess, now, window_seconds=60) is True
    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert age < 60


def test_transcript_recently_active_widens_to_subagent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283: a stale registered transcript but a fresh sibling subagent
    transcript in the same project dir -> recently active (widened lookup)."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-active"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # Registered transcript (surface_ref-prefixed), stale but resolvable.
    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    # Sibling subagent transcript (own session id, NOT surface_ref-prefixed), fresh.
    sib = _write_idle_transcript(home, worktree, filename="subagent-abc.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-active-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is True


def test_transcript_age_seconds_widens_to_subagent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283: age is computed from the FRESHER sibling, not the stale registered
    transcript."""
    from cw.reconcile import _transcript_age_seconds

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-age"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    sib = _write_idle_transcript(home, worktree, filename="subagent-abc.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-age-1", worktree, started_at)

    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - 30) < 5


def test_transcript_recently_active_ignores_stale_prior_session_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 regression guard: a sibling transcript from a PRIOR session (mtime
    before started_at) must NOT count -- the mtime > started_at guard is applied
    per-file across the whole glob, not just the registered transcript."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-guard"
    # Session starts at 00:10; a prior-session sibling's mtime (00:09:30) is
    # recent relative to `now` (00:11:00) but predates this session's start.
    started_at = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 11, 0, tzinfo=UTC)

    # No registered transcript for this session (surface_ref glob finds nothing).
    sib = _write_idle_transcript(home, worktree, filename="prior-subagent.jsonl")
    sib_ts = (started_at - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-guard-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is False


def test_project_transcripts_latest_timestamp_isolates_one_bad_sibling(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 fix-cycle-1 regression guard: a stat() failure on ONE sibling
    transcript must not discard max_ts already found from other, valid
    siblings -- locks the per-candidate (not whole-loop) OSError isolation in
    _project_transcripts_latest_timestamp. Pre-fix, any single bad candidate
    aborted the whole glob scan and returned None."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-partial-fail"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # No registered transcript resolvable for this session (surface_ref glob
    # finds nothing) -- isolates the assertion to the sibling-glob helper.
    good = _write_idle_transcript(home, worktree, filename="subagent-good.jsonl")
    good_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(good, (good_ts, good_ts))
    bad = _write_idle_transcript(home, worktree, filename="subagent-bad.jsonl")
    bad_ts = (now - timedelta(seconds=10)).timestamp()
    os.utime(bad, (bad_ts, bad_ts))

    real_stat = Path.stat
    stat_fail_msg = "simulated stat failure mid-glob"

    def _flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == "subagent-bad.jsonl":
            raise OSError(stat_fail_msg)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    sess = _mk_content_ts_session("widen-partial-fail-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is True


def test_widened_transcript_timestamp_survives_primary_transcript_oserror(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 fix-cycle-1 regression guard: a stat() failure resolving the
    REGISTERED transcript must not prevent the sibling-glob fallback from
    being consulted -- locks the independent-computation ordering in
    _widened_transcript_timestamp. Pre-fix, the primary's OSError propagated
    before project_ts was ever computed."""
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-primary-fail"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # Fresh sibling transcript -- the widened fallback this fix protects.
    sib = _write_idle_transcript(home, worktree, filename="subagent-live.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-primary-fail-1", worktree, started_at)

    # Force registered-transcript resolution to a nonexistent path so
    # _effective_transcript_timestamp's stat() raises OSError -- same
    # injection technique as test_transcript_age_seconds_oserror_returns_none
    # (tests/test_cli.py).
    fake_path = worktree / "does-not-exist.jsonl"
    monkeypatch.setattr(
        "cw.reconcile._shared._locate_session_transcript",
        lambda *_a, **_kw: fake_path,
    )

    assert _transcript_recently_active(sess, now) is True
    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - 30) < 5


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


def test_phantom_reverted_event_emitted_with_dirty_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON phantom revert emits session.phantom_reverted with worktree_dirty=True.

    Uses worktree_path (not session.branch) for dirty detection — DAEMON sessions
    always have branch=None in production (GitHub issue #421).
    """
    wt_path = tmp_path / "wt-pd"
    sess = _mk_session("phantom-dirty", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-PD"
    sess.branch = None  # DAEMON sessions always have branch=None in production
    sess.worktree_path = wt_path
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-PD",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICK-PD",
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
        consumer="test-phantom-dirty",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "phantom-dirty"
    assert p["ticket_id"] == "TICK-PD"
    assert p["client"] == "client-a"
    assert p["worktree_dirty"] is True
    assert p["worktree_path"] == str(wt_path)
    assert events[0].correlation_id == "TICK-PD"


def test_phantom_reverted_event_emitted_with_clean_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON phantom revert emits session.phantom_reverted with dirty=False.

    Uses worktree_path (not session.branch) for dirty detection — DAEMON sessions
    always have branch=None in production (GitHub issue #421).
    """
    wt_path = tmp_path / "wt-pc"
    sess = _mk_session("phantom-clean", "dead-ref-2")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-PC"
    sess.branch = None  # DAEMON sessions always have branch=None in production
    sess.worktree_path = wt_path
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-PC",
        client="client-a",
        status=QueueItemStatus.RUNNING,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICK-PC",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

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
    assert p["worktree_path"] == str(wt_path)
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
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )

    reconcile()

    events = read_events(
        consumer="test-phantom-user-origin",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 0


# ---------------------------------------------------------------------------
# GitHub issue #421 — phantom-sweep dirty-worktree routing tests
# ---------------------------------------------------------------------------


def test_phantom_dirty_worktree_routes_to_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON session with branch=None but worktree_path set, dirty worktree
    routes task to BLOCKED_ON_USER, not PENDING.

    This test MUST use worktree_path for dirtiness, NOT session.branch.
    Asserts session.branch is None so the test fails if impl reads session.branch.
    """
    wt_path = tmp_path / "wt-421-dirty"
    sess = _mk_session("phantom-421-dirty", "dead-ref-421")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-421D"
    sess.branch = None  # Always None on DAEMON sessions — impl must NOT use this
    sess.worktree_path = wt_path
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-421D",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-421-dirty",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICK-421D",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    report = reconcile()

    # Dirty phantom → BLOCKED_ON_USER, NOT PENDING
    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "TICK-421D")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert updated_task.session_id is None  # session stamp cleared

    # BLOCKED_ON_USER ticket must NOT appear in reverted_ticket_ids
    assert "TICK-421D" not in report.reverted_ticket_ids

    # Assert branch was never consulted (it's None, impl must use worktree_path)
    state = load_state()
    matching = [s for s in state.sessions if s.id == "phantom-421-dirty"]
    assert matching, "session should still be in state after reap"
    # (session is COMPLETED/CRASHED after phantom reap, branch is still None)
    assert matching[0].branch is None


def test_phantom_clean_worktree_routes_to_pending(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON session with branch=None and clean worktree routes task to PENDING."""
    wt_path = tmp_path / "wt-421-clean"
    sess = _mk_session("phantom-421-clean", "dead-ref-421c")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-421C"
    sess.branch = None
    sess.worktree_path = wt_path
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-421C",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-421-clean",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICK-421C",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    report = reconcile()

    # Clean phantom → PENDING for retry
    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "TICK-421C")
    assert updated_task.status == QueueItemStatus.PENDING
    assert updated_task.session_id is None

    # Clean-reverted ticket appears in reverted_ticket_ids
    assert "TICK-421C" in report.reverted_ticket_ids


def test_dirty_phantom_task_not_re_claimable(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After dirty phantom routes to BLOCKED_ON_USER, task stays invisible to dispatch.

    BLOCKED_ON_USER tasks are not claimed by _claim_next_pending (it only
    claims PENDING tasks), so the dirty worktree is not re-dispatched.
    """
    wt_path = tmp_path / "wt-421-nore"
    sess = _mk_session("phantom-421-nore", "dead-ref-421n")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TICK-421N"
    sess.branch = None
    sess.worktree_path = wt_path
    save_state(CwState(sessions=[sess]))

    task = TicketTask(
        ticket_id="TICK-421N",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-421-nore",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch",
        lambda _p: "auto-dev/TICK-421N",
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    reconcile()

    # Task is BLOCKED_ON_USER — a subsequent reconcile should not touch it
    reconcile()

    store = load_dev_queue()
    updated_task = next(t for t in store.tasks if t.ticket_id == "TICK-421N")
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER


def test_phantom_reverted_event_carries_queue_status_blocked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SESSION_PHANTOM_REVERTED event: queue_status='blocked_on_user' for dirty."""
    wt_dirty = tmp_path / "wt-qs-dirty"
    sess_d = _mk_session("phantom-qs-d", "dead-qs-d")
    sess_d.origin = SessionOrigin.DAEMON
    sess_d.name = "client-a/auto-dev/TICK-QSD"
    sess_d.branch = None
    sess_d.worktree_path = wt_dirty
    save_state(CwState(sessions=[sess_d]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="TICK-QSD",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/TICK-QSD"
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
        consumer="test-qs-dirty",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    assert events[0].payload["queue_status"] == "blocked_on_user"


def test_phantom_reverted_event_carries_queue_status_pending(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SESSION_PHANTOM_REVERTED event includes queue_status='pending' for clean."""
    wt_clean = tmp_path / "wt-qs-clean"
    sess_c = _mk_session("phantom-qs-c", "dead-qs-c")
    sess_c.origin = SessionOrigin.DAEMON
    sess_c.name = "client-a/auto-dev/TICK-QSC"
    sess_c.branch = None
    sess_c.worktree_path = wt_clean
    save_state(CwState(sessions=[sess_c]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="TICK-QSC",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/TICK-QSC"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    reconcile()

    events = read_events(
        consumer="test-qs-clean",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    assert events[0].payload["queue_status"] == "pending"


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
# dirty-worktree push-notification storm regression tests (GitHub #763)
# ---------------------------------------------------------------------------


def test_dirty_worktree_push_fires_once_not_per_tick_timed_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT session with dirty worktree: push fires exactly once, not per tick.

    The first call routes the RUNNING task to BLOCKED_ON_USER and fires the
    push.  The second call finds the task already BLOCKED_ON_USER (not RUNNING)
    and must not fire again (#763).
    """
    wt_path = tmp_path / "wt-storm-to"
    sess = _mk_daemon_session_with_worktree(
        "storm-to", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-to",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-to",
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-to"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_timed_out_tasks()  # tick 1 — routes to BLOCKED_ON_USER, fires push
    revert_timed_out_tasks()  # tick 2 — task is BLOCKED_ON_USER, must not re-fire
    revert_timed_out_tasks()  # tick 3 — still BLOCKED_ON_USER, must not re-fire

    assert len(push_calls) == 1, (
        f"fire_push_notification must fire exactly once, fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_silent_for_already_blocked_task(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal session with task already BLOCKED_ON_USER never fires push (#763)."""
    wt_path = tmp_path / "wt-storm-ab"
    sess = _mk_daemon_session_with_worktree(
        "storm-ab", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-ab",
                    client="client-a",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id=None,  # already reverted
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-ab"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    for _ in range(3):
        revert_timed_out_tasks()

    assert push_calls == [], (
        f"fire_push_notification must not fire for already-BLOCKED_ON_USER task, "
        f"fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_silent_for_no_task_terminal_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal session with dirty worktree, no task: push never fires (#763)."""
    wt_path = tmp_path / "wt-storm-nt"
    sess = _mk_daemon_session_with_worktree(
        "storm-nt", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(DevQueueStore(tasks=[]))  # no task at all

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-nt"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    for _ in range(3):
        revert_timed_out_tasks()

    assert push_calls == [], (
        f"fire_push_notification must not fire for zombie session with no queue task, "
        f"fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_fires_once_not_per_tick_completed_silent(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED session with dirty worktree: push fires exactly once, not per tick.

    Mirrors test_dirty_worktree_push_fires_once_not_per_tick_timed_out but
    exercises the revert_completed_silent_tasks() code path (#763).
    """
    wt_path = tmp_path / "wt-storm-cs"
    sess = _mk_daemon_session_with_worktree(
        "storm-cs", SessionStatus.COMPLETED, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-cs",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-cs",
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-cs"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_completed_silent_tasks()  # tick 1 — routes to BLOCKED_ON_USER, fires push
    revert_completed_silent_tasks()  # tick 2 — already BLOCKED_ON_USER, no re-fire
    revert_completed_silent_tasks()  # tick 3 — still BLOCKED_ON_USER, no re-fire

    assert len(push_calls) == 1, (
        f"fire_push_notification must fire exactly once on completed-silent path, "
        f"fired {len(push_calls)} times"
    )


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

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

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
        "cw.reconcile._shared.salvage_terminal_result",
        lambda *_args, **_kwargs: (fake_result, "fake-claude-id"),
    )

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

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
        "cw.reconcile._shared.get_client",
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

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

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
# TestCompleteTimedOutMergedTasks — #488
# ---------------------------------------------------------------------------


class TestCompleteTimedOutMergedTasks:
    """complete_timed_out_merged_tasks() auto-completes PENDING tasks on merged PR."""

    def _pending_task(self, ticket_id: str) -> TicketTask:
        return TicketTask(ticket_id=ticket_id, client="client-a")

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


# ---------------------------------------------------------------------------
# TestSalvageCommittedNoPrSessions (GitHub issue #497)
# ---------------------------------------------------------------------------


def _mk_live_daemon_session_with_worktree(
    sid: str,
    worktree: Path,
    ticket_id: str,
) -> Session:
    """Build a live DAEMON ACTIVE session with a headless context and worktree."""
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = Session(
        id=sid,
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=started_at,
    )
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "' + sid + '"}'
    )
    return sess


def _write_stage_event(
    session_id: str,
    stage: str,
    started_at: datetime,
    *,
    offset_seconds: int = 60,
) -> None:
    """Write a STAGE_ENTERED event for testing _detect_post_review_clean."""
    from cw.events import record_event
    from cw.models import OrchestratorEventType

    record_event(
        OrchestratorEventType.STAGE_ENTERED,
        {
            "session_id": session_id,
            "stage": stage,
        },
    )


class TestSalvageCommittedNoPrSessions:
    """Tests for salvage_committed_no_pr_sessions (GitHub issue #497)."""

    def test_high_path_creates_draft_pr_and_completes_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH path: post-review clean + commits + no PR → draft PR created,
        task COMPLETED, SESSION_COMPLETED with salvage_kind=git_state_salvage."""
        worktree = tmp_path / "wt-high"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-HIGH"
        sess = _mk_live_daemon_session_with_worktree("sess-high", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-high",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Write a stage event for post-review clean
        _write_stage_event("sess-high", _STAGE_REVIEW_COMPLETE, sess.started_at)

        push_calls: list[object] = []
        gh_calls: list[object] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            if args[:2] == ["git", "push"]:
                push_calls.append(args)
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                gh_calls.append(args)
                result.stdout = "https://github.com/org/repo/pull/42\n"
                return result
            return result

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        # First call (pre-check): no PR; second call (idempotency): no PR.
        # Capture cwd from both to prove it is scoped to client-a's repo (#1279).
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _capture_pr_exists
        )
        monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client",
            MagicMock,
        )

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-high", ticket_id, "dev/high-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert ticket_id in completed
        # Both the pre-check and the _salvage_high_path recheck are scoped.
        assert pr_cwds == [Path("/tmp/ws-staged"), Path("/tmp/ws-staged")]

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-high-path-salvage",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        salvage_events = [
            e
            for e in events
            if e.payload.get("salvage_kind") == _SALVAGE_KIND_GIT_STATE
        ]
        assert len(salvage_events) == 1
        assert salvage_events[0].payload.get("draft") is True
        assert "github.com" in str(salvage_events[0].payload.get("pr", ""))

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-high")
        assert s.status == SessionStatus.COMPLETED

    def test_low_path_flags_needs_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path: commits + no PR + no post-review clean → needs_salvage,
        task BLOCKED_ON_USER, SESSION_NEEDS_ATTENTION with breadcrumbs."""
        worktree = tmp_path / "wt-low"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW"
        sess = _mk_live_daemon_session_with_worktree("sess-low", worktree, ticket_id)
        sess.lane = "salvage-lane"
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-low", ticket_id, "dev/low-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-low-path-salvage",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1
        bc = attn_events[0].payload.get("breadcrumbs", "")
        assert "dev/low-branch" in str(bc)
        assert str(worktree) in str(bc)
        assert attn_events[0].payload["lane"] == "salvage-lane"

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-low")
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON
        assert s.status == SessionStatus.COMPLETED

    def test_low_path_flags_needs_salvage_lane_none_survives_uncoerced(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path with session.lane=None: payload carries lane=None, not
        coerced to DEFAULT_LANE or any other default (#1333 R4)."""
        worktree = tmp_path / "wt-low-none-lane"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW-NONE-LANE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-low-none-lane", worktree, ticket_id
        )
        assert sess.lane is None  # precondition: default, not stamped
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low-none-lane",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-low-none-lane",
                ticket_id,
                "dev/low-none-lane-branch",
                str(worktree),
                False,
            )
        ]
        salvage_committed_no_pr_sessions(candidates)

        events = read_events(
            consumer="test-low-path-salvage-none-lane",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1
        assert attn_events[0].payload["lane"] is None

    def test_low_path_usage_limit_true_clean_worktree_routes_to_timed_out_cutoff(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path + usage-limit cutoff detected → TIMED_OUT/USAGE_LIMIT_CUTOFF,
        NOT needs_salvage/CRASHED; task stays RUNNING (no BLOCKED_ON_USER); no
        SESSION_NEEDS_ATTENTION false alarm (#1336)."""
        worktree = tmp_path / "wt-ul-clean"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-CLEAN"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-clean", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-clean",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False (LOW path)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.detect_usage_limit", lambda _s: True)
        mock_daemon = MagicMock()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client",
            lambda: mock_daemon,
        )

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-clean", ticket_id, "dev/ul-clean-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-clean")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.completed_reason == CompletionReason.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF
        lr = s.last_result or {}
        assert lr.get("paused_status") != _NEEDS_SALVAGE_REASON

        # Task must NOT be routed to BLOCKED_ON_USER — stays RUNNING for the
        # tasks.py:revert_timed_out_tasks backstop to pick up next tick.
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

        # No SESSION_NEEDS_ATTENTION false alarm for needs_salvage.
        events = read_events(
            consumer="test-ul-clean-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert attn_events == []

        # Daemon surface cleanup still happens on both branches.
        mock_daemon.stop.assert_called_once_with("live-ref")

    def test_low_path_usage_limit_false_keeps_needs_salvage_disposition(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit-mock companion to test_low_path_flags_needs_salvage: with
        detect_usage_limit patched to return False, behavior is unchanged from
        the pre-#1336 needs_salvage disposition."""
        worktree = tmp_path / "wt-ul-false"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-FALSE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-false", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-false",
                    )
                ]
            )
        )
        # No stage event written → post_review_clean=False

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.detect_usage_limit", lambda _s: False)
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-false", ticket_id, "dev/ul-false-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-ul-false-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-false")
        lr = s.last_result or {}
        assert lr.get("paused_status") == _NEEDS_SALVAGE_REASON
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        assert s.status == SessionStatus.COMPLETED
        assert s.completed_reason == CompletionReason.CRASHED

    def test_low_path_usage_limit_true_dirty_worktree_still_parks_via_backstop(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Usage-limit cutoff stamps TIMED_OUT/USAGE_LIMIT_CUTOFF, but a
        dirty worktree still routes the task to BLOCKED_ON_USER via the
        tasks.py:revert_timed_out_tasks backstop — the reap_reason stamped by
        salvage.py survives untouched (#1336)."""
        worktree = tmp_path / "wt-ul-dirty"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UL-DIRTY"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-dirty", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-dirty",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.detect_usage_limit", lambda _s: True)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-ul-dirty", ticket_id, "dev/ul-dirty-branch", str(worktree), False)
        ]
        salvage_committed_no_pr_sessions(candidates)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-dirty")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

        # Now simulate the backstop tick observing a dirty worktree.
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "dev/ul-dirty-branch",
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
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert ticket_id not in reverted

        events = read_events(
            consumer="test-ul-dirty-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        dirty_events = [
            e
            for e in events
            if e.payload.get("paused_status") == _DIRTY_WORKTREE_REASON
        ]
        assert len(dirty_events) == 1
        needs_salvage_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert needs_salvage_events == []

        reloaded2 = load_state()
        s2 = next(s for s in reloaded2.sessions if s.id == "sess-ul-dirty")
        assert s2.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

    def test_revert_timed_out_preserves_worktree_for_usage_limit_salvage_origin(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mandatory regression (#1336): a usage-limit-cutoff-stamped session
        must NOT have its worktree deleted by the revert_timed_out_tasks
        backstop, and a clean worktree must still auto-retry (RUNNING→PENDING)."""
        worktree = tmp_path / "wt-ul-preserve"
        worktree.mkdir(parents=True)
        (worktree / "committed.txt").write_text("work")
        ticket_id = "TKT-UL-PRESERVE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-ul-preserve", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ul-preserve",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.detect_usage_limit", lambda _s: True)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-ul-preserve",
                ticket_id,
                "dev/ul-preserve-branch",
                str(worktree),
                False,
            )
        ]
        salvage_committed_no_pr_sessions(candidates)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-ul-preserve")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.USAGE_LIMIT_CUTOFF

        remove_worktree_calls: list[object] = []
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree",
            lambda *a, **kw: remove_worktree_calls.append((a, kw)),
        )

        # Clean worktree (not a real git repo → checked_out_branch returns
        # None → not dirty) — no dirty patch needed here.
        reverted = revert_timed_out_tasks()

        assert remove_worktree_calls == []
        assert worktree.exists()
        assert (worktree / "committed.txt").exists()

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None
        assert ticket_id in reverted

    def test_low_path_merges_into_existing_last_result(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW path must MERGE paused_status into last_result, not replace it —
        otherwise it destroys the sentinel `status` that `cw dev-queue approve`
        requires (#1105)."""
        from cw.reconcile.salvage import _salvage_low_path

        worktree = tmp_path / "wt-merge"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-MERGE"
        sess = _mk_live_daemon_session_with_worktree("sess-merge", worktree, ticket_id)
        sess.last_result = {
            "status": "review_pending_approval",
            "review": {"clean": True},
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-merge",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        _salvage_low_path(sess, ticket_id, "dev/merge-branch", str(worktree))

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sess-merge")
        assert s.last_result is not None
        assert s.last_result["status"] == "review_pending_approval"
        assert s.last_result["review"] == {"clean": True}
        assert s.last_result["paused_status"] == _NEEDS_SALVAGE_REASON
        assert s.reap_reason == ReapReason.SALVAGE_PARKED
        assert s.status == SessionStatus.COMPLETED

    def test_stalled_needs_salvage_route_stamps_disposition(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """LOW-path salvage's bare transition_task_status call stamps disposition
        (#976 Bug B — salvage.py bare-site regression)."""
        worktree = tmp_path / "wt-low-disp"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-LOW-DISP"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-low-disp", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-low-disp",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-low-disp", ticket_id, "dev/low-disp-branch", str(worktree), False)
        ]
        salvage_committed_no_pr_sessions(candidates)

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == _NEEDS_SALVAGE_REASON

    def test_low_path_idempotent_on_second_pass(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_salvage_low_path called twice for same already-flagged session →
        SESSION_NEEDS_ATTENTION fires exactly once, fire_push_notification
        called exactly once. Guards the self-contained idempotency added in #418."""
        worktree = tmp_path / "wt-idem-low"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-IDEM-LOW"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-idem-low", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-idem-low",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        push_calls: list[tuple[str, str]] = []

        def _capture_push(name: str, client: str, **_kw: object) -> None:
            push_calls.append((name, client))

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates = [
            ("sess-idem-low", ticket_id, "dev/idem-low-branch", str(worktree), False)
        ]

        # First pass — should flag and emit.
        salvage_committed_no_pr_sessions(candidates)
        # Second pass — already_flagged should suppress.
        salvage_committed_no_pr_sessions(candidates)

        events = read_events(
            consumer="test-low-idem",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn_events = [
            e for e in events if e.payload.get("paused_status") == _NEEDS_SALVAGE_REASON
        ]
        assert len(attn_events) == 1, "SESSION_NEEDS_ATTENTION must fire exactly once"
        assert len(push_calls) == 1, (
            "fire_push_notification must be called exactly once"
        )

    def test_idempotency_recheck_pr_now_exists_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR appears on the second pr_exists_for_branch call → downgrade to LOW."""
        worktree = tmp_path / "wt-idem"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-IDEM"
        sess = _mk_live_daemon_session_with_worktree("sess-idem", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-idem",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        call_count = [0]

        def _pr_exists_side_effect(branch: str, **_kw: object) -> tuple[bool, bool]:
            call_count[0] += 1
            if call_count[0] == 1:
                return False, True  # outer check: no PR
            return True, True  # idempotency re-check: PR now exists

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _pr_exists_side_effect
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-idem", ticket_id, "dev/idem-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        # Downgraded to LOW — no PR created, task blocked
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

        # No git push attempted
        # (confirmed by no SESSION_COMPLETED with salvage_kind=git_state_salvage)
        events = read_events(
            consumer="test-idem-recheck",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert all(
            e.payload.get("salvage_kind") != _SALVAGE_KIND_GIT_STATE for e in events
        )

    def test_gh_unavailable_skips_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (None, False) → no salvage, no event."""
        worktree = tmp_path / "wt-gh-absent"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-GHABS"
        sess = _mk_live_daemon_session_with_worktree("sess-ghabs", worktree, ticket_id)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-ghabs",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        candidates = [
            ("sess-ghabs", ticket_id, "dev/ghabs-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged

        events = read_events(
            consumer="test-gh-absent-salvage",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not events

    def test_no_commits_beyond_base_skips_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_has_commits_beyond_base returns False → no salvage."""
        worktree = tmp_path / "wt-no-commits"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-NOCOMMITS"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-nocommits", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-nocommits",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: False
        )

        candidates = [
            ("sess-nocommits", ticket_id, "dev/nc-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged

    def test_worktree_not_deleted_for_salvage_candidates(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sessions in salvage_git list do NOT have remove_worktree called."""
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        worktree = tmp_path / "wt-nodelete"
        worktree.mkdir(parents=True)
        (worktree / ".claude").mkdir()
        (worktree / ".claude" / "cw-context.json").write_text(
            '{"headless": true, "session_id": "sess-nodelete"}'
        )

        ticket_id = "TKT-NODELETE"
        sess = Session(
            id="sess-nodelete",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-nodelete",
                        attempts=5,  # at/above cap → would normally park
                    )
                ]
            )
        )

        remove_worktree_calls: list[object] = []

        def _mock_remove(*args: object, **kwargs: object) -> None:
            remove_worktree_calls.append(args)

        monkeypatch.setattr("cw.reconcile._shared.remove_worktree", _mock_remove)
        # Patch _checked_out_branch to return a valid branch (triggers salvage_git)
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "dev/nodelete-branch",
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.salvage_terminal_result", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile.idle._transcript_recently_active", lambda *_a, **_kw: False
        )
        monkeypatch.setattr(
            "cw.reconcile.idle._awaiting_subagent", lambda *_a, **_kw: False
        )

        state = CwState(sessions=[sess])
        _, salvage_git = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=1),
        )

        # Session ended up in salvage_git, not park
        assert len(salvage_git) == 1
        assert salvage_git[0][0] == "sess-nodelete"

        # remove_worktree was NOT called
        assert remove_worktree_calls == []

    def test_double_fire_guard_skips_needs_salvage_sessions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """revert_stalled_headless_sessions skips sessions with
        last_result={"paused_status": "needs_salvage"}."""
        worktree = tmp_path / "wt-dblfire"
        worktree.mkdir(parents=True)
        (worktree / ".claude").mkdir()
        (worktree / ".claude" / "cw-context.json").write_text(
            '{"headless": true, "session_id": "sess-dblfire"}'
        )

        ticket_id = "TKT-DBLFIRE"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        sess = Session(
            id="sess-dblfire",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
            last_result={"paused_status": _NEEDS_SALVAGE_REASON},
        )
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id="sess-dblfire",
                    )
                ]
            )
        )

        reverted = revert_stalled_headless_sessions(
            state, now=now, config=_auto_config()
        )

        # Session was skipped — not reverted to PENDING
        assert ticket_id not in reverted
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_time_window_stale_event_does_not_trigger_high(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale s3_review_complete event (before session.started_at) → LOW path."""
        from cw.events import record_event as _record_event

        worktree = tmp_path / "wt-timewindow"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-TIMEWINDOW"
        # Session started AFTER the event was recorded
        # (simulate: event is from a prior session)
        started_at = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
        sess = Session(
            id="sess-timewindow",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="live-ref",
            started_at=started_at,
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Write event with a timestamp AFTER session started
        # (but _detect_post_review_clean uses since_ts=session.started_at
        #  and checks session_id match)
        # The event has a DIFFERENT session_id — should not trigger HIGH
        _record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {
                "session_id": "different-session-id",  # wrong session
                "stage": _STAGE_REVIEW_COMPLETE,
            },
        )

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        # post_review_clean=False (different session_id → _detect_post_review_clean
        # returns False → LOW path)
        candidates = [
            ("sess-timewindow", ticket_id, "dev/tw-branch", str(worktree), False)
        ]
        completed = salvage_committed_no_pr_sessions(candidates)

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_session_not_in_state_is_skipped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Candidate session_id not found in state → silently skipped."""
        save_state(CwState(sessions=[]))  # empty — no sessions
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        candidates = [
            (
                "sess-missing",
                "TKT-MISSING",
                "dev/missing-branch",
                str(tmp_path),
                True,
            )
        ]
        completed = salvage_committed_no_pr_sessions(candidates)
        assert completed == []

    def test_pr_transient_error_skips_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (None, True) → skip candidate."""
        worktree = tmp_path / "wt-transient"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-TRANSIENT"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-transient", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-transient",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        # (None, True) = transient error, gh available
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, True)
        )

        completed = salvage_committed_no_pr_sessions(
            [("sess-transient", ticket_id, "dev/t-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_pr_already_exists_skips_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr_exists_for_branch returns (True, True) → PR exists, skip."""
        worktree = tmp_path / "wt-prexists"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-PREXISTS"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-prexists", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-prexists",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (True, True)
        )

        completed = salvage_committed_no_pr_sessions(
            [("sess-prexists", ticket_id, "dev/pe-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_salvage_skips_session_with_unknown_client(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Unknown client → CwError caught, session skipped, completed empty."""
        worktree = tmp_path / "wt-unknown-client"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-UNKNOWNCLIENT"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-unknownclient", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-unknownclient",
                    )
                ]
            )
        )
        # Intentionally no _write_staged_clients_yaml call → get_client raises CwError

        completed = salvage_committed_no_pr_sessions(
            [("sess-unknownclient", ticket_id, "dev/uc-branch", str(worktree), True)]
        )

        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING  # unchanged — session skipped

    def test_git_push_failure_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git push failure in HIGH path → downgrade to LOW (BLOCKED_ON_USER)."""
        worktree = tmp_path / "wt-pushfail"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-PUSHFAIL"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-pushfail", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-pushfail",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        def _subprocess_push_fails(args: list[str], **_kw: object) -> None:
            if args[:2] == ["git", "push"]:
                raise subprocess.CalledProcessError(1, args)
            msg = f"unexpected call: {args}"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.subprocess.run", _subprocess_push_fails
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-pushfail", ticket_id, "dev/pf-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_gh_pr_create_failure_downgrades_to_low(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr create failure in HIGH path → downgrade to LOW (BLOCKED_ON_USER)."""
        worktree = tmp_path / "wt-createfail"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-CREATEFAIL"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-createfail", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-createfail",
                    )
                ]
            )
        )

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        def _subprocess_create_fails(args: list[str], **_kw: object) -> MagicMock:
            if args[:2] == ["git", "push"]:
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                raise subprocess.CalledProcessError(1, args)
            msg = f"unexpected call: {args}"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.subprocess.run", _subprocess_create_fails
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
        )
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-createfail", ticket_id, "dev/cf-branch", str(worktree), True)]
        )
        assert completed == []
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_high_path_uses_client_default_branch_not_main(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH path uses client's default_branch in gh pr create.

        Regression: hardcoded 'main' was replaced by the client's
        default_branch in the --base arg.
        """
        worktree = tmp_path / "wt-devbranch"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-DEVBRANCH"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-devbranch", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-devbranch",
                    )
                ]
            )
        )

        # Write a client config with default_branch=develop (not main)
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-staged\n"
            "    default_branch: develop\n"
            "    pipeline:\n"
            "      stages: [plan, impl, review, finalize]\n"
        )

        _write_stage_event("sess-devbranch", _STAGE_REVIEW_COMPLETE, sess.started_at)

        gh_base_args: list[str] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            if args[:2] == ["git", "push"]:
                result.stdout = ""
                return result
            if args[:2] == ["gh", "pr"]:
                gh_base_args.extend(args)
                result.stdout = "https://github.com/org/repo/pull/77\n"
                return result
            return result

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        completed = salvage_committed_no_pr_sessions(
            [("sess-devbranch", ticket_id, "dev/devbranch", str(worktree), True)]
        )

        assert ticket_id in completed
        # Verify --base uses the client's default_branch, not "main"
        assert "--base" in gh_base_args
        base_idx = gh_base_args.index("--base")
        assert gh_base_args[base_idx + 1] == "develop"

    def test_salvage_committed_no_pr_skips_merged_ticket(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids short-circuits before pr_exists_for_branch (#1054).

        A ticket whose PR already merged is shipped ground truth -- no PR
        should be created, and it must not appear in the completed list
        (completion is owned by the idle merged path, not this salvage post-pass).
        """
        worktree = tmp_path / "wt-merged-salvage"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-MERGED-SALVAGE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-merged-salvage", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-merged-salvage",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            ("sess-merged-salvage", ticket_id, "dev/merged-branch", str(worktree), True)
        ]
        completed = salvage_committed_no_pr_sessions(
            candidates, merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id not in completed
        pr_check.assert_not_called()

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.RUNNING

    def test_salvage_committed_no_pr_different_client_ticket_not_skipped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_client_ticket_ids for a DIFFERENT client must not skip this
        session's same-numbered ticket (cross-client collision guard, #1054).
        """
        worktree = tmp_path / "wt-collision-salvage"
        worktree.mkdir(parents=True)
        ticket_id = "TKT-COLLIDE-SALVAGE"
        sess = _mk_live_daemon_session_with_worktree(
            "sess-collide-salvage", worktree, ticket_id
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="sess-collide-salvage",
                    )
                ]
            )
        )
        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        monkeypatch.setattr(
            "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

        candidates: list[tuple[str, str | None, str, str, bool]] = [
            (
                "sess-collide-salvage",
                ticket_id,
                "dev/collide-branch",
                str(worktree),
                False,  # post_review_clean=False -> LOW path, no gh subprocess
            )
        ]
        # client-b's same-numbered ticket is merged; this session is client-a's.
        salvage_committed_no_pr_sessions(
            candidates, merged_client_ticket_ids=frozenset({("client-b", ticket_id)})
        )

        pr_check.assert_called_once()


# ---------------------------------------------------------------------------
# TestDetectPostReviewClean
# ---------------------------------------------------------------------------


class TestDetectPostReviewClean:
    """Unit tests for _detect_post_review_clean."""

    def test_returns_false_when_worktree_path_none(self, tmp_config_dir: Path) -> None:
        """Session with no worktree_path → False."""
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-nopath",
            name="client-a/sess-nopath",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert _detect_post_review_clean(sess) is False

    def test_returns_true_when_matching_event_present(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Event with correct session_id and stage=s3_review_complete → True."""
        from cw.events import record_event as _record_event
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-match",
            name="client-a/sess-match",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        _record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {"session_id": "sess-match", "stage": _STAGE_REVIEW_COMPLETE},
        )
        assert _detect_post_review_clean(sess) is True

    def test_returns_false_when_no_matching_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No matching event → False."""
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-nomatch",
            name="client-a/sess-nomatch",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        # No event written
        assert _detect_post_review_clean(sess) is False

    def test_returns_false_on_read_events_exception(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_events raises → safe False."""
        from cw.reconcile import _detect_post_review_clean

        def _raise(*_a: object, **_kw: object) -> None:
            msg = "disk error"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._shared.read_events", _raise)

        sess = Session(
            id="sess-exc",
            name="client-a/sess-exc",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert _detect_post_review_clean(sess) is False


# ---------------------------------------------------------------------------
# _locate_session_transcript tests (GitHub #541)
# ---------------------------------------------------------------------------


def _make_locate_session(
    *,
    worktree: Path,
    started_at: datetime,
    surface_ref: str | None = "abcd1234",
    claude_session_id: str | None = None,
) -> Session:
    """Build a minimal DAEMON ACTIVE session for locate-transcript tests."""
    return Session(
        id="test-locate",
        name="client-a/impl",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref=surface_ref,
        claude_session_id=claude_session_id,
        started_at=started_at,
    )


def test_locate_by_csid_returns_path(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id set and file exists → returns that path."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-csid"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        claude_session_id="full-csid-uuid",
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "full-csid-uuid.jsonl"
    transcript.write_text("{}\n")

    result = _locate_session_transcript(sess)
    assert result == transcript


def test_locate_by_csid_missing_file(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id set but file absent → None."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-csid-missing"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        claude_session_id="missing-csid",
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    # No transcript written.

    result = _locate_session_transcript(sess)
    assert result is None


def test_locate_by_surface_ref_precise_glob(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref set, matching file mtime > started_at → returns Path."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-sref"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "abcd1234-full-uuid.jsonl"
    transcript.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    result = _locate_session_transcript(sess)
    assert result == transcript


def test_locate_excludes_sibling_by_prefix(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused-worktree scenario: sibling transcript (different surface_ref,
    mtime NEWER than target's started_at) does NOT win; the target's own
    transcript is returned.  Proves the prefix-scoping (not just the mtime
    guard) excludes the sibling.  See GitHub #541.
    """
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-sibling"
    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)

    # Sibling transcript: DIFFERENT surface_ref prefix, but mtime AFTER started_at.
    # If only the mtime guard were applied (not the prefix glob), this would be
    # picked as the newest candidate.
    sibling = project_dir / "zzzzzzzz-other-session.jsonl"
    sibling.write_text("{}\n")
    sibling_ts = started_at.timestamp() + 120  # newer than target's started_at
    os.utime(str(sibling), (sibling_ts, sibling_ts))

    # Target transcript: correct surface_ref prefix, mtime after started_at.
    target = project_dir / "abcd1234-full-uuid.jsonl"
    target.write_text("{}\n")
    target_ts = started_at.timestamp() + 60  # after started_at, but older than sibling
    os.utime(str(target), (target_ts, target_ts))

    result = _locate_session_transcript(sess)
    # Must return the TARGET transcript, not the newer sibling.
    assert result == target
    assert result != sibling


def test_locate_stale_mtime_returns_none(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref set, matching file exists but mtime <= started_at → None."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-stale-sref"
    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "abcd1234-uuid.jsonl"
    transcript.write_text("{}\n")
    # Stamp BEFORE started_at.
    stale_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (stale_ts, stale_ts))

    result = _locate_session_transcript(sess)
    assert result is None


def test_locate_no_surface_ref_no_csid(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both surface_ref and claude_session_id are None → None (no fallback)."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-no-ids"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref=None,
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    # Write a transcript that would be picked by an unscoped *.jsonl glob.
    unscoped = project_dir / "some-uuid.jsonl"
    unscoped.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(unscoped), (after_ts, after_ts))

    # Should NOT fall back to unscoped glob — returns None.
    result = _locate_session_transcript(sess)
    assert result is None


def test_awaiting_subagent_stale_transcript_returns_false(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale transcript (mtime <= started_at) with pending tool_use → False.

    Before #541, _awaiting_subagent used an unscoped *.jsonl glob with no
    mtime guard in the surface_ref branch.  A stale transcript with a pending
    tool_use tail would have returned True (false-positive watchdog suppression).
    Now the mtime guard applies uniformly via _locate_session_transcript.
    """
    from cw.reconcile import _awaiting_subagent

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-stale-subagent"
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)

    # Write a transcript with a pending tool_use tail.
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": now.isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}],
            },
        }
    )
    transcript = project_dir / "abcd1234-stale.jsonl"
    transcript.write_text(record + "\n")
    # Stamp BEFORE started_at — stale transcript guard should fire.
    stale_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (stale_ts, stale_ts))

    # Must return False: stale transcript → helper returns None → fail-open.
    assert _awaiting_subagent(sess, now) is False


def test_csid_from_transcript_via_helper(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_csid_from_transcript delegates to _locate_session_transcript; returns stem."""
    from cw.reconcile import _csid_from_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-csid-helper"
    surface_ref = "abcd1234"
    full_stem = f"{surface_ref}-full-uuid-xxxx"
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref=surface_ref,
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / f"{full_stem}.jsonl"
    transcript.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    result = _csid_from_transcript(sess)
    assert result == full_stem


# ---------------------------------------------------------------------------
# ReapReason taxonomy tests (GitHub #380)
# One test per reap-site decision-table row.
# ---------------------------------------------------------------------------


def test_reap_reason_phantom_surface(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_reconcile_locked phantom sweep sets reap_reason=phantom_surface."""
    sess = _mk_session("phantom-s1", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/PHANTOM-1"
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="PHANTOM-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="phantom-s1",
                )
            ]
        )
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    reconcile()

    reloaded = load_state()
    s = reloaded.find_by_name_or_id("phantom-s1")
    assert s is not None
    assert s.reap_reason == ReapReason.PHANTOM_SURFACE


def test_reap_reason_wall_clock_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """revert_stalled_headless_sessions sets reap_reason=wall_clock_budget."""
    worktree = tmp_path / "wt-wc"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("wc-budget-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "wc-budget-1")
    assert s.reap_reason == ReapReason.WALL_CLOCK_BUDGET


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
        patch("cw.reconcile._shared.detect_usage_limit", return_value=False),
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
        patch("cw.reconcile._shared.detect_usage_limit", return_value=True),
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


def test_reap_reason_salvage_completed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_salvage_high_path sets reap_reason=salvage_completed on the session."""
    worktree = tmp_path / "wt-salv-comp"
    worktree.mkdir(parents=True)
    ticket_id = "SALV-COMP-1"

    sess = Session(
        id="salv-comp-1",
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref-sc",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-comp-1",
                )
            ]
        )
    )

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "https://github.com/org/repo/pull/99\n"
        return result

    monkeypatch.setattr(
        "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
    )
    monkeypatch.setattr(
        "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
    )
    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

    candidates: list[tuple[str, str | None, str, str, bool]] = [
        ("salv-comp-1", ticket_id, "dev/salv-comp", str(worktree), True)
    ]
    salvage_committed_no_pr_sessions(candidates)

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "salv-comp-1")
    assert s.reap_reason == ReapReason.SALVAGE_COMPLETED


def test_reap_reason_salvage_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_salvage_low_path sets reap_reason=salvage_parked on the session."""
    worktree = tmp_path / "wt-salv-park"
    worktree.mkdir(parents=True)
    ticket_id = "SALV-PARK-1"

    sess = Session(
        id="salv-park-1",
        name=f"client-a/auto-dev/{ticket_id}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref="live-ref-sp",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-park-1",
                )
            ]
        )
    )

    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    monkeypatch.setattr(
        "cw.reconcile.salvage._has_commits_beyond_base", lambda _p, _b: True
    )
    monkeypatch.setattr(
        "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
    )
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", MagicMock)

    candidates: list[tuple[str, str | None, str, str, bool]] = [
        ("salv-park-1", ticket_id, "dev/salv-park", str(worktree), False)
    ]
    salvage_committed_no_pr_sessions(candidates)

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "salv-park-1")
    assert s.reap_reason == ReapReason.SALVAGE_PARKED
    assert s.status == SessionStatus.COMPLETED


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
# Phase detect/act split tests (GitHub #552)
# ---------------------------------------------------------------------------


# --- _detect_stalled_candidates ---


def test_detect_stalled_candidates_under_budget_returns_empty(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Session with elapsed < budget → no candidate, bytes unchanged."""
    from cw.reconcile import _detect_stalled_candidates

    worktree = tmp_path / "wt-under"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("under-detect", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_revert_task_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """ACTIVE DAEMON headless session with elapsed > budget → REVERT_TASK candidate."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("revert-detect-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    task = TicketTask(
        ticket_id="revert-detect-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="revert-detect-1",
    )
    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"revert-detect-1": task},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.REVERT_TASK
    assert c.ticket_id == "revert-detect-1"
    assert c.reap_reason == ReapReason.WALL_CLOCK_BUDGET
    # Purity: no writes
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_skip_parked_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """session.last_result = {"paused_status": "silently_idle"} → SKIP_PARKED."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-skip"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("skip-parked-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SKIP_PARKED
    assert candidates[0].paused_status == _SILENTLY_IDLE_REASON
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_salvage_completion_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session past budget with terminal sentinel → SALVAGE_COMPLETION."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-salvage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salv-detect-1", worktree, started_at)
    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "salv-detect-1"
    _write_salvage_transcript(home, worktree, "csid-uuid", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    assert c.salvage_result is not None
    assert _state_queue_snapshot() == snap


def test_detect_stalled_candidates_skips_user_origin(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """USER-origin session → not in returned list; bytes unchanged."""
    from cw.reconcile import _detect_stalled_candidates

    worktree = tmp_path / "wt-user"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("user-origin-1", worktree, started_at)
    sess.origin = SessionOrigin.USER
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert candidates == []
    assert _state_queue_snapshot() == snap


# --- _detect_idle_candidates ---


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


def test_detect_phantom_candidates_crash_complete(
    tmp_config_dir: Path,
) -> None:
    """DAEMON session in phantom_set, no sentinel → CRASH_COMPLETE."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-crash-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.ticket_id == "phantom-crash-1"
    assert _state_queue_snapshot() == snap


def test_detect_phantom_candidates_skips_emitted_terminal_result(
    tmp_config_dir: Path,
) -> None:
    """#536: a phantom with a terminal last_result is left for the operator.

    An emit-then-crash session (``cw result emit`` already persisted a terminal
    result, then the surface died) is never re-salvaged or re-crashed over its
    authoritative emit-time result — the gate ``continue``s past it. A sibling
    phantom with no emitted result still yields CRASH_COMPLETE, so the gate
    does not over-fire.
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    gated = _mk_phantom_daemon_session("phantom-emit-1", started_at)
    gated.last_result = {"status": "shipped"}
    ungated = _mk_phantom_daemon_session("phantom-emit-2", started_at)
    state = CwState(sessions=[gated, ungated])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_phantom_candidates(
        state,
        phantom_set={gated.id, ungated.id},
    )

    # The emitted-terminal session is skipped entirely (no candidate of any kind).
    assert all(c.session_id != gated.id for c in candidates)
    # The sibling without an emitted result still crashes.
    ungated_candidates = [c for c in candidates if c.session_id == ungated.id]
    assert len(ungated_candidates) == 1
    assert ungated_candidates[0].proposed_action == ProposedAction.CRASH_COMPLETE
    # Purity: detection makes zero writes.
    assert _state_queue_snapshot() == snap


def test_detect_phantom_candidates_salvage_on_terminal_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON session in phantom_set with terminal sentinel → SALVAGE_COMPLETION."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-phantom-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # surface_ref must match the prefix used by _write_salvage_transcript
    sess = _mk_phantom_daemon_session(
        "phantom-salv-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "phantom-salv-1"
    _write_salvage_transcript(home, worktree, "csid-salv", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SALVAGE_COMPLETION
    assert _state_queue_snapshot() == snap


def test_detect_phantom_candidates_user_origin_crash_no_ticket(
    tmp_config_dir: Path,
) -> None:
    """USER session in phantom_set → CRASH_COMPLETE, ticket_id is None."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = Session(
        id="phantom-user-1",
        name="client-a/impl",  # no auto-dev/ prefix → no ticket_id
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.USER,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        surface_ref="dead-ref",
        started_at=started_at,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.ticket_id is None
    assert _state_queue_snapshot() == snap


def test_detect_phantom_candidates_worktree_dirty_on_candidate(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom with dirty worktree → candidate.worktree_dirty True; bytes unchanged."""
    from cw.reconcile import _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    wt = tmp_path / "wt-dirty"
    wt.mkdir()
    sess = _mk_phantom_daemon_session("phantom-dirty-1", started_at, worktree_path=wt)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_dirty_by_path", lambda _c, _p: True
    )

    candidates = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
    )

    assert len(candidates) == 1
    assert candidates[0].worktree_dirty is True
    assert _state_queue_snapshot() == snap


def test_detect_phantom_candidates_usage_limit_detected_true(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAEMON phantom with usage-limit transcript → usage_limit_detected=True (#804)."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-ul-phantom"
    sess = _mk_phantom_daemon_session(
        "phantom-ul-1",
        started_at,
        surface_ref="ul-phantom-ref",
        worktree_path=worktree,
    )
    transcript = _write_idle_transcript_with_text(
        home,
        worktree,
        "You've hit your session limit · resets 5:00am",
        filename="ul-phantom-ref-sess-804.jsonl",
    )
    # Timestamp transcript after session start so _detect_usage_limit finds it.
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(state, phantom_set={sess.id})

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.usage_limit_detected is True


def test_detect_phantom_candidates_usage_limit_false_when_no_transcript(
    tmp_config_dir: Path,
) -> None:
    """DAEMON phantom with no transcript → usage_limit_detected=False (#804)."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-noul-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(state, phantom_set={sess.id})

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.usage_limit_detected is False


def test_phantom_route_emitted_sentinel_refusal_stops_refiring(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1149: a phantom-path stage-mismatch refusal stamps the
    paused_status-only marker so the doomed ROUTE_EMITTED_SENTINEL candidate
    is not re-proposed on a subsequent detect pass -- it falls through to the
    ordinary CRASH_COMPLETE construction instead.
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates
    from cw.reconcile.phantom import _apply_phantom_routed_mutations

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-phantom-refusal"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session(
        "phantom-refusal-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "phantom-refusal-1"
    _write_salvage_transcript(home, worktree, "csid-phantom-refusal", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="phantom-refusal-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-refusal-1",
        # Row already advanced past IMPL by the time this stale IMPL-leg
        # sentinel is discovered -- the #986 shape.
        stage=Stage.REVIEW,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-refusal-1": task}
    )
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL

    session_by_id = {s.id: s for s in state.sessions}
    accepted = _apply_phantom_routed_mutations(
        session_by_id, candidates, now=started_at, phantom_names=[]
    )

    assert accepted == []
    reloaded = session_by_id["phantom-refusal-1"]
    assert reloaded.status != SessionStatus.COMPLETED
    assert reloaded.last_result == {
        "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
    }
    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "phantom-refusal-1"
    )
    assert task_after.stage == Stage.REVIEW
    assert task_after.status == QueueItemStatus.RUNNING

    # Second detect pass over the now-marked session: the already_refused
    # skip check stops offering the same doomed advance candidate -- it
    # falls through to CRASH_COMPLETE instead.
    candidates_2 = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-refusal-1": task_after}
    )
    assert len(candidates_2) == 1
    assert candidates_2[0].proposed_action == ProposedAction.CRASH_COMPLETE


def test_phantom_route_emitted_sentinel_refusal_marker_is_not_terminal_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the idle-path equivalent: the phantom-path refusal marker
    carries no "status" key, so _has_terminal_sentinel stays False."""
    from cw.reconcile import _detect_phantom_candidates
    from cw.reconcile.phantom import _apply_phantom_routed_mutations

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-phantom-not-terminal"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session(
        "phantom-nt-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    payload = _stage_complete_payload()
    payload["ticket_id"] = "phantom-nt-1"
    _write_salvage_transcript(home, worktree, "csid-phantom-nt", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="phantom-nt-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-nt-1",
        stage=Stage.REVIEW,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-nt-1": task}
    )
    session_by_id = {s.id: s for s in state.sessions}
    _apply_phantom_routed_mutations(
        session_by_id, candidates, now=started_at, phantom_names=[]
    )

    reloaded = session_by_id["phantom-nt-1"]
    assert reloaded.last_result == {
        "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
    }
    assert _has_terminal_sentinel(reloaded) is False


def test_phantom_route_emitted_sentinel_refusal_preserves_existing_park_marker(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage-mismatch refusal must not clobber a pre-existing paused_status
    marker from another sweep, AND must still latch the refusal so the doomed
    candidate isn't re-offered forever (GitHub #1149 review findings).

    Unlike idle.py (whose detect phase only builds a ROUTE_EMITTED_SENTINEL
    candidate when session.last_result is already None), phantom.py's detect
    phase has no such precondition -- a session already legitimately parked by
    idle.py's watchdog (_SILENTLY_IDLE_REASON) can reach the refusal branch
    with last_result already set. Overwriting it wholesale would destroy that
    marker and defeat stalled.py's SKIP_PARKED check (which reads
    last_result.get("paused_status") for exactly that reason), silently
    un-parking a session another sweep correctly parked -- but *skipping* the
    stamp entirely in that case would re-open the refusal-loop this stamp
    exists to close (the doomed candidate would be re-offered every tick
    forever, since already_refused could never become True). The fix merges a
    second, independent flag (_SENTINEL_ADVANCE_REFUSED_KEY) into the existing
    dict instead of choosing between the two.
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates
    from cw.reconcile.phantom import _apply_phantom_routed_mutations

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-phantom-preserve-marker"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session(
        "phantom-preserve-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    # Already parked by idle.py's watchdog on a prior tick.
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "phantom-preserve-1"
    _write_salvage_transcript(home, worktree, "csid-phantom-preserve", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="phantom-preserve-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-preserve-1",
        # Row already advanced past IMPL by the time this stale IMPL-leg
        # sentinel is discovered -- the #986 shape -- so this refuses.
        stage=Stage.REVIEW,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-preserve-1": task}
    )
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL

    session_by_id = {s.id: s for s in state.sessions}
    accepted = _apply_phantom_routed_mutations(
        session_by_id, candidates, now=started_at, phantom_names=[]
    )

    assert accepted == []
    reloaded = session_by_id["phantom-preserve-1"]
    # The pre-existing idle-watchdog park marker must survive the refusal --
    # not be overwritten -- with the refusal flag merged in alongside it.
    assert reloaded.last_result == {
        "paused_status": _SILENTLY_IDLE_REASON,
        "sentinel_advance_refused": True,
    }
    assert _has_terminal_sentinel(reloaded) is False

    # Second detect pass over the now-marked session: already_refused must
    # still stop offering the same doomed advance candidate even though the
    # marker's paused_status value is idle.py's, not the #1149 refusal
    # reason -- it falls through to CRASH_COMPLETE instead.
    candidates_2 = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-preserve-1": task}
    )
    assert len(candidates_2) == 1
    assert candidates_2[0].proposed_action == ProposedAction.CRASH_COMPLETE


def test_phantom_later_stage_sentinel_routes_forward_instead_of_looping(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1149 R1: a later-stage sentinel (a legitimate self-escalation
    the row hasn't caught up to) routes forward via the shared staged-advance
    authority -- it does NOT hit the #1149 R4 refusal branch, so no marker is
    stamped and the session completes normally.
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates
    from cw.reconcile.phantom import _apply_phantom_routed_mutations

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-phantom-later"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session(
        "phantom-later-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "phantom-later-1"
    _write_salvage_transcript(home, worktree, "csid-phantom-later", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="phantom-later-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-later-1",
        # PLAN is EARLIER than the sentinel's mapped IMPL stage -> "later"
        # position: a legitimate self-escalation, walked forward.
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, task_by_ticket={"phantom-later-1": task}
    )
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL

    session_by_id = {s.id: s for s in state.sessions}
    accepted = _apply_phantom_routed_mutations(
        session_by_id, candidates, now=started_at, phantom_names=[]
    )

    assert len(accepted) == 1
    reloaded = session_by_id["phantom-later-1"]
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result.get("paused_status") is None

    # Walk PLAN -> IMPL (matching sentinel's stage), then Rule 3's
    # stage_complete advance moves IMPL -> REVIEW; session_id cleared by the
    # landing Rule's own genuine advance.
    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "phantom-later-1"
    )
    assert task_after.stage == Stage.REVIEW
    assert task_after.status == QueueItemStatus.PENDING
    assert task_after.session_id is None


def test_act_on_phantom_candidates_propagates_usage_limited(
    tmp_config_dir: Path,
) -> None:
    """_act_on_phantom_candidates returns usage_limited=True when a CRASH_COMPLETE
    candidate carries usage_limit_detected=True (#804)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-ul-act-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-ul-act-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-ul-act-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-ul-act-1",
        proposed_action=ProposedAction.CRASH_COMPLETE,
        ticket_id="phantom-ul-act-1",
        worktree_dirty=False,
        usage_limit_detected=True,
        client="client-a",
        worktree_path=None,
    )

    _, _, usage_limited, _, _, _ = _act_on_phantom_candidates(
        state, [candidate], now=now, config=_auto_config()
    )

    assert usage_limited is True


def test_act_on_phantom_candidates_usage_limited_false_without_flag(
    tmp_config_dir: Path,
) -> None:
    """_act_on_phantom_candidates returns usage_limited=False when no candidate
    has usage_limit_detected=True (#804)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-noul-act-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-noul-act-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-noul-act-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-noul-act-1",
        proposed_action=ProposedAction.CRASH_COMPLETE,
        ticket_id="phantom-noul-act-1",
        worktree_dirty=False,
        usage_limit_detected=False,
        client="client-a",
        worktree_path=None,
    )

    _, _, usage_limited, _, _, _ = _act_on_phantom_candidates(
        state, [candidate], now=now, config=_auto_config()
    )

    assert usage_limited is False


def test_act_on_phantom_candidates_signal_only_still_propagates_usage_limited(
    tmp_config_dir: Path,
) -> None:
    """Under signal_only (default) policy, a CRASH_COMPLETE candidate with
    usage_limit_detected=True is routed to BLOCKED_ON_USER (filtered from
    auto-reap), but usage_limited=True is still returned in position 2 (#804).

    This exercises the early-return path at line 538 of phantom.py that
    returns `usage_limited` instead of hard-coded `False`."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-ul-so-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-ul-so-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-ul-so-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-ul-so-1",
        proposed_action=ProposedAction.CRASH_COMPLETE,
        ticket_id="phantom-ul-so-1",
        worktree_dirty=False,
        usage_limit_detected=True,
        client="client-a",
        worktree_path=None,
    )

    # OrchestratorConfig() has reap_policy=SIGNAL_ONLY (the default), which
    # routes clean CRASH_COMPLETE candidates to BLOCKED_ON_USER and removes
    # them from the auto-reap list — triggering the early-return on line 538.
    _, _, usage_limited, _, _, _ = _act_on_phantom_candidates(
        state, [candidate], now=now, config=OrchestratorConfig()
    )

    assert usage_limited is True


# --- Act dispatcher tests ---


def test_act_on_stalled_revert_task_updates_state_and_queue(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVERT_TASK candidate → TIMED_OUT session, PENDING queue, event emitted."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-act-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None)

    sess = _mk_headless_daemon_session("act-revert-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-revert-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-revert-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="act-revert-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="act-revert-1",
        elapsed_seconds=3700.0,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )

    reverted, _ = _act_on_stalled_candidates(
        state, [candidate], now=now, config=_auto_config()
    )

    assert "act-revert-1" in reverted
    assert sess.status == SessionStatus.TIMED_OUT

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-revert-1")
    assert t.status == QueueItemStatus.PENDING

    events = read_events(
        consumer="test-act-revert-1",
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
    )
    assert len(events) == 1
    # Must NOT emit SESSION_COMPLETED for REVERT_TASK path
    completed_events = read_events(
        consumer="test-act-revert-1-completed",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(completed_events) == 0


def test_act_on_stalled_salvage_completion(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION candidate → session COMPLETED, queue COMPLETED."""
    from cw.auto_dev_result import AutoDevResult
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    worktree = tmp_path / "wt-act-salv"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-salv-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-salv-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-salv-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    result = AutoDevResult.model_validate(_shipped_salvage_payload())
    candidate = ReapCandidate(
        session_id="act-salv-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="act-salv-1",
        salvage_result=result,
        salvage_csid="csid-act-salv",
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    assert sess.status == SessionStatus.COMPLETED
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-salv-1")
    assert t.status == QueueItemStatus.COMPLETED


def test_act_on_stalled_skip_parked_emits_event_no_queue_change(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """SKIP_PARKED candidate → SESSION_SALVAGE_SKIPPED emitted; queue unchanged."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-skip-act"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-skip-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-skip-1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="act-skip-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="act-skip-1",
        proposed_action=ProposedAction.SKIP_PARKED,
        ticket_id="act-skip-1",
        paused_status=_SILENTLY_IDLE_REASON,
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    # Queue must be unchanged
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-skip-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer="test-act-skip-1",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1


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


def test_act_on_phantom_crash_routes_pending(
    tmp_config_dir: Path,
) -> None:
    """CRASH_COMPLETE (dirty=False) → queue PENDING + SESSION_PHANTOM_REVERTED."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-act-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-act-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-act-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-act-1",
        proposed_action=ProposedAction.CRASH_COMPLETE,
        ticket_id="phantom-act-1",
        worktree_dirty=False,
        client="client-a",
        worktree_path=None,
    )

    result = _act_on_phantom_candidates(
        state, [candidate], now=now, config=_auto_config()
    )
    _, _, _, _, _, _ = result

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "phantom-act-1")
    assert t.status == QueueItemStatus.PENDING

    events = read_events(
        consumer="test-phantom-act-1",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    assert events[0].payload["worktree_dirty"] is False


def test_act_on_phantom_dirty_routes_blocked(
    tmp_config_dir: Path,
) -> None:
    """CRASH_COMPLETE (dirty=True) → BLOCKED_ON_USER + SESSION_PHANTOM_REVERTED."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-act-dirty-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-act-dirty-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-act-dirty-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-act-dirty-1",
        proposed_action=ProposedAction.CRASH_COMPLETE,
        ticket_id="phantom-act-dirty-1",
        worktree_dirty=True,
        client="client-a",
        worktree_path=None,
    )

    _act_on_phantom_candidates(state, [candidate], now=now)

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "phantom-act-dirty-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer="test-phantom-dirty-1",
        event_types=[OrchestratorEventType.SESSION_PHANTOM_REVERTED],
    )
    assert len(events) == 1
    assert events[0].payload["worktree_dirty"] is True


# ---------------------------------------------------------------------------
# Additional act-phase tests addressing coverage gaps (GitHub #552 fix cycle 1)
# ---------------------------------------------------------------------------


def test_act_on_stalled_salvage_completion_emits_session_completed(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION → SESSION_COMPLETED event emitted with salvaged=True."""
    from cw.auto_dev_result import AutoDevResult
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    worktree = tmp_path / "wt-salv-evt"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-salv-evt-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "act-salv-evt-1"
    result = AutoDevResult.model_validate(payload)
    candidate = ReapCandidate(
        session_id="act-salv-evt-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="act-salv-evt-1",
        salvage_result=result,
        salvage_csid="csid-salv-evt",
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    events = read_events(
        consumer="test-act-salv-evt-1",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].payload["salvaged"] is True
    assert events[0].payload["crashed"] is False


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


def test_act_on_phantom_salvage_completion_routes_queue_and_emits_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION phantom → session COMPLETED, queue COMPLETED, event."""
    from cw.auto_dev_result import AutoDevResult
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client",
        FakeNativeDaemonClient,
    )

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session("phantom-salv-act-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-salv-act-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-salv-act-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "phantom-salv-act-1"
    result = AutoDevResult.model_validate(payload)
    candidate = ReapCandidate(
        session_id="phantom-salv-act-1",
        proposed_action=ProposedAction.SALVAGE_COMPLETION,
        ticket_id="phantom-salv-act-1",
        salvage_result=result,
        salvage_csid="csid-phantom-salv",
        client="client-a",
        worktree_path=None,
    )

    _, _, _, salvaged_ids, _, _ = _act_on_phantom_candidates(
        state, [candidate], now=now
    )

    assert sess.status == SessionStatus.COMPLETED
    assert "phantom-salv-act-1" in salvaged_ids

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "phantom-salv-act-1")
    assert t.status == QueueItemStatus.COMPLETED

    events = read_events(
        consumer="test-phantom-salv-act-1",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].payload["salvaged"] is True
    assert events[0].payload["crashed"] is False


def test_detect_stalled_needs_salvage_reason_skip_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """session.last_result with _NEEDS_SALVAGE_REASON → SKIP_PARKED candidate."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    worktree = tmp_path / "wt-needs-salvage"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("needs-salvage-1", worktree, started_at)
    sess.last_result = {"paused_status": _NEEDS_SALVAGE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))
    snap = _state_queue_snapshot()

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SKIP_PARKED
    assert candidates[0].paused_status == _NEEDS_SALVAGE_REASON
    assert _state_queue_snapshot() == snap


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


def test_stalled_veto_suppresses_park_when_transcript_fresh(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget expired but transcript fresh -> PARK_VETOED, not REVERT_TASK;
    no queue/session mutation through the act phase (#976)."""
    from cw.reconcile import (
        ProposedAction,
        _act_on_stalled_candidates,
        _detect_stalled_candidates,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-fresh"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("veto-fresh-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-fresh-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-fresh-1",
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    # 5 minutes stale — well under the 15-min PLAN-stage floor -> LIVE.
    fresh_ts = (now - timedelta(minutes=5)).timestamp()
    os.utime(str(transcript), (fresh_ts, fresh_ts))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"veto-fresh-1": task},
    )

    veto = next(
        c for c in candidates if c.proposed_action == ProposedAction.PARK_VETOED
    )
    assert veto.stale_minutes is not None
    assert veto.stale_minutes < 15
    assert not any(c.proposed_action == ProposedAction.REVERT_TASK for c in candidates)

    _act_on_stalled_candidates(state, candidates, now=now, config=_auto_config())

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "veto-fresh-1")
    assert t.status == QueueItemStatus.RUNNING

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "veto-fresh-1")
    assert s.status == SessionStatus.ACTIVE


def test_stalled_veto_park_proceeds_once_quiet(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget expired and transcript stale beyond the per-stage floor ->
    normal REVERT_TASK still fires (#976)."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-quiet"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("veto-quiet-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-quiet-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-quiet-1",
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    # 40 minutes stale — past the 15-min PLAN-stage floor -> not LIVE.
    stale_ts = (now - timedelta(minutes=40)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"veto-quiet-1": task},
    )

    assert any(c.proposed_action == ProposedAction.REVERT_TASK for c in candidates)
    assert not any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)


def test_stalled_veto_applies_to_cap_exceeded_park_when_transcript_fresh(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempts >= cap with a fresh transcript -> PARK_VETOED, not
    PARK_BLOCKED_ON_USER — the veto is now reachable from the cap-exceeded
    branch too (#1277). Previously this branch parked unconditionally
    because the veto was only ever consulted from the wall-clock revert
    path, which the cap-exceeded branch always short-circuits before
    reaching."""
    from cw.reconcile import (
        DEFAULT_STALLED_RETRY_CAP,
        ProposedAction,
        _detect_stalled_candidates,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-cap"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("veto-cap-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-cap-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-cap-1",
        stage=Stage.PLAN,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    fresh_ts = (now - timedelta(minutes=1)).timestamp()
    os.utime(str(transcript), (fresh_ts, fresh_ts))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"veto-cap-1": task},
    )

    veto = next(
        c for c in candidates if c.proposed_action == ProposedAction.PARK_VETOED
    )
    assert veto.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED
    assert not any(
        c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER for c in candidates
    )


def test_stalled_veto_cap_exceeded_matches_1266_regression_signature(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (detect + act) regression guard for #1277: a cap-exceeded,
    wall-clock-expired session with a fresh transcript must be vetoed all
    the way through the act phase — task stays RUNNING, session stays
    ACTIVE, and a SESSION_PARK_VETOED event is emitted with the cap-sourced
    reap reason, not silently parked BLOCKED_ON_USER."""
    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP, HEADLESS_TIMEOUT_SECONDS

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-cap-e2e"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=3655.8)
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session("veto-cap-e2e-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-cap-e2e-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-cap-e2e-1",
        stage=Stage.PLAN,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    stale_ts = (now - timedelta(seconds=45)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    reverted = revert_stalled_headless_sessions(state, now=now, config=_auto_config())

    assert "veto-cap-e2e-1" not in reverted

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "veto-cap-e2e-1")
    assert t.status == QueueItemStatus.RUNNING

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "veto-cap-e2e-1")
    assert s.status == SessionStatus.ACTIVE

    events = read_events(
        consumer="test-veto-cap-e2e-1",
        event_types=[OrchestratorEventType.SESSION_PARK_VETOED],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "veto-cap-e2e-1"
    assert payload["reason"] == "stalled_retry_cap_parked"


def test_stalled_veto_cap_exceeded_stale_transcript_still_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: a genuinely dead cap-exceeded session (transcript
    present but stale past the per-stage floor) must still park
    BLOCKED_ON_USER — the new liveness check inside the cap branch must not
    override a real timeout (#1277)."""
    from cw.reconcile import (
        DEFAULT_STALLED_RETRY_CAP,
        ProposedAction,
        _detect_stalled_candidates,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-cap-stale"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("veto-cap-stale-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-cap-stale-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-cap-stale-1",
        stage=Stage.PLAN,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    # 40 minutes stale — past the 15-min PLAN-stage floor -> not LIVE.
    stale_ts = (now - timedelta(minutes=40)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"veto-cap-stale-1": task},
    )

    park = next(
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    )
    assert park.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED
    assert not any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)


def test_act_on_stalled_park_vetoed_emits_event_no_mutation(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """PARK_VETOED candidate -> session.park_vetoed emitted; zero mutation (#976)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-veto-act"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("act-veto-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="act-veto-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="act-veto-1",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="act-veto-1",
        proposed_action=ProposedAction.PARK_VETOED,
        ticket_id="act-veto-1",
        elapsed_seconds=3700.0,
        client="client-a",
        stage=Stage.IMPL,
        stale_minutes=4.2,
        # Why: stamp explicitly (#1277) so this exercises the post-fix
        # "read from candidate.reap_reason" emission path, not the
        # defensive None-fallback that now exists alongside it.
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "act-veto-1")
    assert t.status == QueueItemStatus.RUNNING
    assert t.disposition is None

    s = next(s for s in state.sessions if s.id == "act-veto-1")
    assert s.status == SessionStatus.ACTIVE

    events = read_events(
        consumer="test-act-veto-1",
        event_types=[OrchestratorEventType.SESSION_PARK_VETOED],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "act-veto-1"
    assert payload["client"] == "client-a"
    assert payload["session_id"] == "act-veto-1"
    assert payload["reason"] == "wall_clock_budget"
    assert payload["stale_minutes"] == 4.2
    assert events[0].correlation_id == "act-veto-1"


# ---------------------------------------------------------------------------
# GitHub #1149 Path 1 backstop -- stalled.py harvests a same-/later-stage
# stage_complete advance sentinel instead of wall-clock-reverting it.
# ---------------------------------------------------------------------------


def test_stalled_wall_clock_reap_harvests_stage_complete_instead_of_reverting(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall-clock-expired session whose transcript carries a same-stage
    stage_complete sentinel is harvested (ROUTE_EMITTED_SENTINEL) instead of
    reverted -- `stage_complete` is excluded from SALVAGE_TERMINAL_STATUSES so
    the ordinary salvage check never sees it (#1149 Path 1 backstop)."""
    from cw.reconcile import ProposedAction, _act_on_stalled_candidates
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-harvest"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-harvest", worktree, started_at)
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "1149-harvest"
    _write_salvage_transcript(home, worktree, "claude-1149-harvest", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-harvest",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-harvest",
        stage=Stage.IMPL,  # matches the sentinel's mapped stage -> "same"
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state, now=now, config=_auto_config(), task_by_ticket={"1149-harvest": task}
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    assert not any(c.proposed_action == ProposedAction.REVERT_TASK for c in candidates)
    assert not any(
        c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER for c in candidates
    )

    reverted, merged_completed = _act_on_stalled_candidates(
        state, candidates, now=now, config=_auto_config()
    )

    assert reverted == []
    assert merged_completed == []
    reloaded = next(s for s in load_state().sessions if s.id == "1149-harvest")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL

    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "1149-harvest"
    )
    assert task_after.stage == Stage.REVIEW
    assert task_after.status == QueueItemStatus.PENDING
    assert task_after.session_id is None


def test_stalled_backstop_later_stage_sentinel_harvested_and_walked(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall-clock-expired session whose transcript carries a LATER-stage
    stage_complete sentinel (task row hasn't caught up to a legitimate
    self-escalation) is also harvested -- `_stalled_advance_sentinel_
    candidate` accepts both "same" and "later" positions, not just "same".
    Exercises the previously-untested "later" branch of its `if position not
    in ("same", "later"): return None` gate (#1149 review finding)."""
    from cw.reconcile import ProposedAction, _act_on_stalled_candidates
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-harvest-later"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-harvest-later", worktree, started_at)
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "1149-harvest-later"
    _write_salvage_transcript(home, worktree, "claude-1149-harvest-later", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-harvest-later",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-harvest-later",
        # PLAN is EARLIER than the sentinel's mapped IMPL stage -> "later"
        # position from the sentinel's perspective: a legitimate
        # self-escalation the row hasn't caught up to.
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"1149-harvest-later": task},
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    assert not any(c.proposed_action == ProposedAction.REVERT_TASK for c in candidates)

    reverted, merged_completed = _act_on_stalled_candidates(
        state, candidates, now=now, config=_auto_config()
    )

    assert reverted == []
    assert merged_completed == []
    reloaded = next(s for s in load_state().sessions if s.id == "1149-harvest-later")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL

    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "1149-harvest-later"
    )
    # Walk PLAN -> IMPL (matching sentinel's stage), then Rule 3's own
    # stage_complete advance moves IMPL -> REVIEW.
    assert task_after.stage == Stage.REVIEW
    assert task_after.status == QueueItemStatus.PENDING
    assert task_after.session_id is None


def test_stalled_backstop_excludes_terminal_statuses_already_salvaged(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal sentinel (status=shipped, in SALVAGE_TERMINAL_STATUSES) is
    caught by the existing salvage check BEFORE the Path 1 backstop runs --
    exactly one candidate (SALVAGE_COMPLETION), never a duplicate
    ROUTE_EMITTED_SENTINEL for the same session."""
    from cw.reconcile import ProposedAction
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-salvaged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-salvaged", worktree, started_at)
    payload = _shipped_salvage_payload()
    payload["ticket_id"] = "1149-salvaged"
    _write_salvage_transcript(home, worktree, "claude-1149-salvaged", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="1149-salvaged",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-salvaged",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state, now=now, config=_auto_config(), task_by_ticket={"1149-salvaged": task}
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.SALVAGE_COMPLETION
    assert not any(
        c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL for c in candidates
    )


def test_stalled_backstop_reset_salvage_skip_latch(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session recovering via the Path 1 backstop with a nonzero
    consecutive_salvage_skips latch also yields a RESET_SALVAGE_SKIP_COUNTER
    candidate (mirrors the other 5 non-SKIP_PARKED detect-phase exits, #974)."""
    from cw.reconcile import ProposedAction
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-reset-latch"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-reset-latch", worktree, started_at)
    sess.consecutive_salvage_skips = 2
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1149-reset-latch"
    _write_salvage_transcript(home, worktree, "claude-1149-reset-latch", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-reset-latch",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-reset-latch",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"1149-reset-latch": task},
    )

    assert any(
        c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL for c in candidates
    )
    reset_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.RESET_SALVAGE_SKIP_COUNTER
    ]
    assert len(reset_candidates) == 1
    assert reset_candidates[0].session_id == "1149-reset-latch"


def test_stalled_backstop_bypasses_signal_only_policy(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Path 1 backstop's ROUTE_EMITTED_SENTINEL candidate is exempt from
    signal_only -- it completes the task rather than being routed to
    BLOCKED_ON_USER (R7 bypass: ROUTE_EMITTED_SENTINEL is not a REVERT_TASK,
    so _route_stalled_by_policy's else branch passes it through unchanged)."""
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-sigonly"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-sigonly", worktree, started_at)
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1149-sigonly"
    _write_salvage_transcript(home, worktree, "claude-1149-sigonly", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-sigonly",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-sigonly",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    signal_only_config = OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY)
    candidates = _detect_stalled_candidates(
        state, now=now, config=signal_only_config, task_by_ticket={"1149-sigonly": task}
    )

    from cw.reconcile import _act_on_stalled_candidates

    _act_on_stalled_candidates(state, candidates, now=now, config=signal_only_config)

    reloaded = next(s for s in load_state().sessions if s.id == "1149-sigonly")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL

    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "1149-sigonly"
    )
    # Advanced/PENDING, never routed to BLOCKED_ON_USER by the signal_only gate.
    assert task_after.status != QueueItemStatus.BLOCKED_ON_USER


def test_stalled_backstop_emits_session_completed_with_salvaged_true(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_events: Callable[..., list[CapturedEvent]],
) -> None:
    """The Path 1 backstop's act phase emits SESSION_COMPLETED with
    salvaged=True and status=<sentinel status>, mirroring the alive-idle and
    phantom routed-sentinel event shapes."""
    from cw.reconcile import _act_on_stalled_candidates
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-event"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    completed_events = capture_events(
        "cw.reconcile.dispositions", OrchestratorEventType.SESSION_COMPLETED
    )

    sess = _mk_headless_daemon_session("1149-event", worktree, started_at)
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1149-event"
    _write_salvage_transcript(home, worktree, "claude-1149-event", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-event",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-event",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state, now=now, config=_auto_config(), task_by_ticket={"1149-event": task}
    )
    _act_on_stalled_candidates(state, candidates, now=now, config=_auto_config())

    assert len(completed_events) == 1
    _etype, payload_out, _corr = completed_events[0]
    assert payload_out["session_id"] == "1149-event"
    assert payload_out["salvaged"] is True
    assert payload_out["status"] == "stage_complete"
    assert payload_out["crashed"] is False


def test_stalled_backstop_candidate_routed_at_act_phase(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the act-phase wiring: _act_on_stalled_candidates consumes the Path
    1 backstop's ROUTE_EMITTED_SENTINEL candidate, completes the session, and
    the mutation is persisted -- a fresh load_state() reflects it."""
    from cw.reconcile import _act_on_stalled_candidates
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-act-wiring"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-act-wiring", worktree, started_at)
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1149-act-wiring"
    _write_salvage_transcript(home, worktree, "claude-1149-act-wiring", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-act-wiring",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-act-wiring",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state, now=now, config=_auto_config(), task_by_ticket={"1149-act-wiring": task}
    )
    _act_on_stalled_candidates(state, candidates, now=now, config=_auto_config())

    # Re-read from disk -- proves the mutation was actually persisted, not
    # just applied to the in-memory `state` object passed in.
    reloaded = next(s for s in load_state().sessions if s.id == "1149-act-wiring")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert "status" in reloaded.last_result


def test_stalled_race_already_failed_task_does_not_complete_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1189: a task raced to FAILED by a concurrent caller must not be
    completed by the Path 1 backstop's ROUTE_EMITTED_SENTINEL act phase.

    Same shape as ``test_stalled_backstop_candidate_routed_at_act_phase``,
    except the dev-queue task has already been landed FAILED/abandoned for
    this same ticket/session by the time the act phase's own lookup runs
    (the R3(a) lookup-miss race). The candidate is still built at detect time
    (detection does not check task.status), but the act phase's routed=False
    must drop it from the accepted list rather than completing the session.
    """
    from cw.reconcile import _act_on_stalled_candidates
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1189-stalled-race"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage

    sess = _mk_headless_daemon_session("1189-stalled-race", worktree, started_at)
    payload = _stage_complete_payload()
    payload["ticket_id"] = "1189-stalled-race"
    _write_salvage_transcript(home, worktree, "claude-1189-stalled-race", payload)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1189-stalled-race",
        client="client-a",
        # Already raced to terminal FAILED by a concurrent caller before the
        # act phase's own lookup runs.
        status=QueueItemStatus.FAILED,
        session_id="1189-stalled-race",
        stage=Stage.IMPL,
        disposition="abandoned",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"1189-stalled-race": task},
    )
    _act_on_stalled_candidates(state, candidates, now=now, config=_auto_config())

    reloaded = next(s for s in load_state().sessions if s.id == "1189-stalled-race")
    assert reloaded.status != SessionStatus.COMPLETED

    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "1189-stalled-race"
    )
    assert task_after.status == QueueItemStatus.FAILED
    assert task_after.disposition == "abandoned"


def test_stalled_backstop_earlier_stage_sentinel_falls_through_to_revert(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier-stage (stale replay) stage_complete sentinel is refused by
    the Path 1 backstop's own stage-position check -- ``_stalled_advance_
    sentinel_candidate`` returns None, so detection falls through unchanged
    to the ordinary wall-clock REVERT_TASK path (#1019 preserved)."""
    from cw.reconcile import ProposedAction
    from cw.reconcile.stalled import _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-1149-earlier-fallthrough"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(seconds=7300)  # past every per-stage
    # headless_timeout_by_stage default (PLAN 3600/IMPL 4200/REVIEW 7200/
    # FINALIZE 5400) -- HEADLESS_TIMEOUT_SECONDS alone is the PLAN-only
    # global fallback and understates the budget once task.stage is set.

    sess = _mk_headless_daemon_session("1149-earlier-fallthrough", worktree, started_at)
    payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
    payload["ticket_id"] = "1149-earlier-fallthrough"
    transcript = _write_salvage_transcript(
        home, worktree, "claude-1149-earlier", payload
    )
    # Backdate the transcript well past the per-stage liveness floor (default
    # 15 min) so the wall-clock candidate is a genuine REVERT_TASK, not
    # PARK_VETOED (#976) -- isolates the #1149 stage-position refusal from
    # the unrelated liveness-veto mechanism.
    stale_ts = (now - timedelta(minutes=40)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    state = CwState(sessions=[sess])
    save_state(state)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    task = TicketTask(
        ticket_id="1149-earlier-fallthrough",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="1149-earlier-fallthrough",
        # REVIEW is LATER than the sentinel's mapped IMPL stage -> "earlier"
        # position from the sentinel's perspective: a stale replay. Refuse.
        stage=Stage.REVIEW,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"1149-earlier-fallthrough": task},
    )

    assert not any(
        c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL for c in candidates
    )
    revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]
    assert len(revert_candidates) == 1

    from cw.reconcile import _act_on_stalled_candidates

    reverted, _merged_completed = _act_on_stalled_candidates(
        state, candidates, now=now, config=_auto_config()
    )

    assert "1149-earlier-fallthrough" in reverted
    reloaded = next(
        s for s in load_state().sessions if s.id == "1149-earlier-fallthrough"
    )
    assert reloaded.status == SessionStatus.TIMED_OUT
    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "1149-earlier-fallthrough"
    )
    assert task_after.status == QueueItemStatus.PENDING
    # Untouched -- the refusal never reached _apply_sentinel_to_task.
    assert task_after.stage == Stage.REVIEW


class TestActOnStalledCandidatesSignalOnly:
    """Under signal_only policy, REVERT_TASK stalled candidates → BLOCKED_ON_USER."""

    def test_signal_only_routes_revert_task_to_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal_only: REVERT_TASK → BLOCKED_ON_USER; no stop, no worktree-remove."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-so-stalled"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        removed: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree",
            lambda _c, branch, **_kw: removed.append(branch),
        )

        sess = _mk_headless_daemon_session("so-stalled-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="so-stalled-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="so-stalled-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="so-stalled-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="so-stalled-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        # signal_only is the default — explicit for clarity
        reverted, _ = _act_on_stalled_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        # Should return early: no reverts
        assert reverted == []
        # Task routes to BLOCKED_ON_USER (not PENDING)
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "so-stalled-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == ReapReason.WALL_CLOCK_BUDGET.value
        # Daemon stop NOT called
        assert daemon.stop_calls == []
        # Worktree NOT removed
        assert removed == []

    def test_signal_only_idempotent(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second call with already-BLOCKED_ON_USER task → no additional save."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-so-idem"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_headless_daemon_session("so-idem-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        # Already BLOCKED_ON_USER — not RUNNING, so _apply_queue_mutations skips it
        task = TicketTask(
            ticket_id="so-idem-1",
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id="so-idem-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="so-idem-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="so-idem-1",
            elapsed_seconds=3700.0,
        )

        # Call twice — second call is a no-op (task not RUNNING)
        cfg = OrchestratorConfig()
        _act_on_stalled_candidates(state, [candidate], now=now, config=cfg)
        _act_on_stalled_candidates(state, [candidate], now=now, config=cfg)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "so-idem-1")
        # Still BLOCKED_ON_USER — second call didn't double-write
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_auto_policy_still_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUTO policy: REVERT_TASK still routes to PENDING (regression guard)."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-auto-stalled"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
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

        sess = _mk_headless_daemon_session("auto-stalled-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="auto-stalled-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="auto-stalled-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="auto-stalled-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="auto-stalled-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        reverted, _ = _act_on_stalled_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert "auto-stalled-1" in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "auto-stalled-1")
        assert t.status == QueueItemStatus.PENDING


class TestSalvageSkipAttentionLatch:
    """Per-session consecutive salvage-skip latch (RFC 0007 Phase 4, closes #974).

    Sibling of TestActOnIdleCandidatesSignalOnly / TestActOnStalledCandidatesSignalOnly:
    the session-keyed counter increments once per SKIP_PARKED candidate, fires
    session.needs_attention exactly once at the configured threshold (latch: no
    re-fire while still at/above threshold), and resets to 0 on any of the 5
    non-SKIP_PARKED detect-phase exits.
    """

    # --- act phase: _record_salvage_skip via _act_on_stalled_candidates ---

    def test_skip_below_threshold_no_attention_emit(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """One SKIP_PARKED candidate below threshold increments but does not emit."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-below"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-below-1", worktree, started_at)
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="sk-below-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-below-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=2)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sk-below-1")
        assert s.consecutive_salvage_skips == 1

        events = read_events(
            consumer="test-974-below",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert events == []

    def test_skip_threshold_emits_full_payload(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Reaching the threshold emits session.needs_attention with all 9 fields."""
        from cw.reconcile import (
            _SALVAGE_SKIP_ESCALATED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-thresh"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-thresh-1", worktree, started_at)
        sess.consecutive_salvage_skips = 1
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="sk-thresh-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-thresh-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=2)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sk-thresh-1")
        assert s.consecutive_salvage_skips == 2

        events = read_events(
            consumer="test-974-threshold",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload == {
            "session_id": "sk-thresh-1",
            "session_name": sess.name,
            "client": "client-a",
            "ticket_id": "sk-thresh-1",
            "claude_session_id": None,
            "paused_status": _SALVAGE_SKIP_ESCALATED_REASON,
            "breadcrumbs": (
                f"2 consecutive salvage-skips; last reason: {_SALVAGE_SKIP_REASON}"
            ),
            "crashed": False,
            "lane": DEFAULT_LANE,
        }
        assert events[0].correlation_id == "sk-thresh-1"

    def test_no_refire_while_latched(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A session already at/above threshold does not re-emit on a later skip."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-latch"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-latch-1", worktree, started_at)
        sess.consecutive_salvage_skips = 2  # already at threshold
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="sk-latch-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-latch-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=2)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sk-latch-1")
        assert s.consecutive_salvage_skips == 3

        events = read_events(
            consumer="test-974-latched",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert events == []

    def test_no_push_notification_on_threshold_emit(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The salvage-skip escalation never calls fire_push_notification."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-nopush"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-nopush-1", worktree, started_at)
        sess.consecutive_salvage_skips = 1
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        push_calls: list[object] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification",
            lambda *a, **kw: push_calls.append((a, kw)),
        )

        candidate = ReapCandidate(
            session_id="sk-nopush-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-nopush-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=2)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "sk-nopush-1")
        assert s.consecutive_salvage_skips == 2
        assert push_calls == []

    def test_reset_candidate_zeroes_counter(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A RESET_SALVAGE_SKIP_COUNTER candidate zeroes the session's counter."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-reset-1"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)  # under budget

        sess = _mk_headless_daemon_session("reset-1", worktree, started_at)
        sess.consecutive_salvage_skips = 3
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="reset-1",
            proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
            ticket_id="reset-1",
        )

        _act_on_stalled_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "reset-1")
        assert s.consecutive_salvage_skips == 0

    def test_act_on_stalled_candidates_zeroes_counter_when_reset_is_sole_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Regression lock (#974 plan-review bug fix): a RESET-only tick must
        still reach save_state — verified via a real load_state() round trip,
        not just inspecting the in-memory candidate list."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-reset-sole"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reset-sole-1", worktree, started_at)
        sess.consecutive_salvage_skips = 4
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="reset-sole-1",
            proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
            ticket_id="reset-sole-1",
        )

        result = _act_on_stalled_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )
        assert result == ([], [])

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "reset-sole-1")
        assert s.consecutive_salvage_skips == 0

    def test_act_on_stalled_candidates_persists_increment_when_skip_is_sole_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Regression lock (#974 plan-review bug fix): a SKIP_PARKED-only tick
        must still reach save_state — verified via a real load_state() round
        trip (this was the original silently-dropped-mutation bug)."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-skip-sole"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("skip-sole-1", worktree, started_at)
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="skip-sole-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="skip-sole-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )

        result = _act_on_stalled_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )
        assert result == ([], [])

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "skip-sole-1")
        assert s.consecutive_salvage_skips == 1

    # --- #1283: SESSION_SALVAGE_SKIPPED edge-trigger (once per parked episode) ---

    def test_salvage_skipped_emits_once_per_parked_episode(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """#1283: two consecutive SKIP_PARKED ticks emit SESSION_SALVAGE_SKIPPED
        exactly once (0->1 edge only), not once per tick."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-episode"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-episode-1", worktree, started_at)
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="sk-episode-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-episode-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=99)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)
        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        s = next(s for s in load_state().sessions if s.id == "sk-episode-1")
        assert s.consecutive_salvage_skips == 2

        events = read_events(
            consumer="test-1283-episode",
            event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
        )
        assert len(events) == 1

    def test_salvage_skipped_still_emits_on_first_occurrence(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """#1283 guard against over-throttling: the very first skip of a fresh
        episode (0->1) still fires SESSION_SALVAGE_SKIPPED."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-first"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-first-1", worktree, started_at)
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        candidate = ReapCandidate(
            session_id="sk-first-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-first-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=99)

        _act_on_stalled_candidates(state, [candidate], now=now, config=config)

        s = next(s for s in load_state().sessions if s.id == "sk-first-1")
        assert s.consecutive_salvage_skips == 1

        events = read_events(
            consumer="test-1283-first",
            event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
        )
        assert len(events) == 1

    def test_salvage_skipped_resumes_after_recovery_and_reflag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """#1283: after a recovery (counter reset to 0) a re-park's first skip
        (0->1 again) fires SESSION_SALVAGE_SKIPPED anew -- per-episode, not
        permanent-after-first-ever."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-sk-resume"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("sk-resume-1", worktree, started_at)
        # Prior episode already climbed past its first skip.
        sess.consecutive_salvage_skips = 3
        sess.last_result = {"paused_status": _SALVAGE_SKIP_REASON}
        state = CwState(sessions=[sess])
        save_state(state)

        reset_candidate = ReapCandidate(
            session_id="sk-resume-1",
            proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
            ticket_id="sk-resume-1",
        )
        skip_candidate = ReapCandidate(
            session_id="sk-resume-1",
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id="sk-resume-1",
            paused_status=_SALVAGE_SKIP_REASON,
        )
        config = OrchestratorConfig(salvage_skip_attention_threshold=99)

        # Recovery zeroes the latch, then a fresh park's first skip re-fires.
        _act_on_stalled_candidates(state, [reset_candidate], now=now, config=config)
        _act_on_stalled_candidates(state, [skip_candidate], now=now, config=config)

        s = next(s for s in load_state().sessions if s.id == "sk-resume-1")
        assert s.consecutive_salvage_skips == 1

        events = read_events(
            consumer="test-1283-resume",
            event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
        )
        assert len(events) == 1

    # --- detect phase: RESET_SALVAGE_SKIP_COUNTER appended at all 5 exits ---

    def test_detect_reset_appended_when_under_budget_and_nonzero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Exit 1/5 (bare `continue`, elapsed < budget) appends a reset candidate."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        worktree = tmp_path / "wt-detect-reset-under"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("detect-reset-under-1", worktree, started_at)
        sess.consecutive_salvage_skips = 2
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidates = _detect_stalled_candidates(
            state, now=now, config=_auto_config(), task_by_ticket={}
        )

        assert len(candidates) == 1
        assert (
            candidates[0].proposed_action == ProposedAction.RESET_SALVAGE_SKIP_COUNTER
        )
        assert candidates[0].session_id == "detect-reset-under-1"

    def test_detect_no_reset_when_under_budget_and_zero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A session already at 0 does not grow the candidate list (exit 1/5)."""
        from cw.reconcile import _detect_stalled_candidates

        worktree = tmp_path / "wt-detect-noreset-under"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session(
            "detect-noreset-under-1", worktree, started_at
        )
        assert sess.consecutive_salvage_skips == 0
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidates = _detect_stalled_candidates(
            state, now=now, config=_auto_config(), task_by_ticket={}
        )

        assert candidates == []

    def test_detect_reset_appended_alongside_salvage_completion(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit 2/5 (SALVAGE_COMPLETION) also appends a reset candidate."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-detect-reset-salvage"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("detect-reset-salv-1", worktree, started_at)
        sess.consecutive_salvage_skips = 1
        payload = _shipped_salvage_payload()
        payload["ticket_id"] = "detect-reset-salv-1"
        _write_salvage_transcript(home, worktree, "csid-uuid-974", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidates = _detect_stalled_candidates(
            state, now=now, config=_auto_config(), task_by_ticket={}
        )

        assert len(candidates) == 2
        actions = {c.proposed_action for c in candidates}
        assert actions == {
            ProposedAction.SALVAGE_COMPLETION,
            ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
        }

    def test_detect_reset_appended_alongside_revert_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Exit 5/5 (REVERT_TASK, loop falls through) also appends a reset."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        worktree = tmp_path / "wt-detect-reset-revert"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session(
            "detect-reset-revert-1", worktree, started_at
        )
        sess.consecutive_salvage_skips = 1
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        task = TicketTask(
            ticket_id="detect-reset-revert-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="detect-reset-revert-1",
        )
        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=_auto_config(),
            task_by_ticket={"detect-reset-revert-1": task},
        )

        assert len(candidates) == 2
        actions = {c.proposed_action for c in candidates}
        assert actions == {
            ProposedAction.REVERT_TASK,
            ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
        }

    def test_stalled_veto_resets_salvage_skip_latch(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Veto path still appends RESET_SALVAGE_SKIP_COUNTER alongside
        PARK_VETOED (#976)."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        worktree = tmp_path / "wt-veto-reset"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("veto-reset-1", worktree, started_at)
        sess.consecutive_salvage_skips = 1
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        task = TicketTask(
            ticket_id="veto-reset-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="veto-reset-1",
            stage=Stage.PLAN,
        )

        transcript = _write_idle_transcript(home, worktree)
        fresh_ts = (now - timedelta(minutes=2)).timestamp()
        os.utime(str(transcript), (fresh_ts, fresh_ts))

        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=_auto_config(),
            task_by_ticket={"veto-reset-1": task},
        )

        assert len(candidates) == 2
        actions = {c.proposed_action for c in candidates}
        assert actions == {
            ProposedAction.PARK_VETOED,
            ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
        }

    def test_detect_reset_appended_alongside_park_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Exit 4/5 (PARK_BLOCKED_ON_USER, retry cap) also appends a reset."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        worktree = tmp_path / "wt-detect-reset-park"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("detect-reset-park-1", worktree, started_at)
        sess.consecutive_salvage_skips = 1
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        task = TicketTask(
            ticket_id="detect-reset-park-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="detect-reset-park-1",
            attempts=99,  # >= DEFAULT_STALLED_RETRY_CAP
        )
        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=_auto_config(),
            task_by_ticket={"detect-reset-park-1": task},
        )

        assert len(candidates) == 2
        actions = {c.proposed_action for c in candidates}
        assert actions == {
            ProposedAction.PARK_BLOCKED_ON_USER,
            ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
        }

    def test_detect_reset_appended_alongside_park_finalize_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit 3/5 (PARK_FINALIZE_BLOCKED) also appends a reset candidate."""
        from cw.reconcile import ProposedAction, _detect_stalled_candidates

        worktree = tmp_path / "wt-detect-reset-fb"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "detect-reset-fb-1"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = Session(
            id="fb-sess-974",
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="surf-ref-974",
            started_at=started_at,
            consecutive_salvage_skips=1,
        )
        context_dir = worktree / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            '{"headless": true, "session_id": "fb-sess-974"}'
        )
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-974",
            stage=Stage.FINALIZE,
        )

        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        branch = f"dev/{ticket_id}"
        finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] = {
            branch: (False, True)
        }

        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=_auto_config(),
            task_by_ticket={ticket_id: task},
            finalize_pr_by_branch=finalize_pr_by_branch,
        )

        assert len(candidates) == 2
        actions = {c.proposed_action for c in candidates}
        assert actions == {
            ProposedAction.PARK_FINALIZE_BLOCKED,
            ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
        }

    def test_reset_candidate_passes_through_signal_only_routing_unchanged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """RESET_SALVAGE_SKIP_COUNTER is not REVERT_TASK, so SIGNAL_ONLY routing
        (which only re-routes REVERT_TASK) must not touch it."""
        from cw.reconcile import ProposedAction, ReapCandidate
        from cw.reconcile.stalled import _route_stalled_by_policy

        reset_candidate = ReapCandidate(
            session_id="route-reset-1",
            proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
            ticket_id="route-reset-1",
        )

        routed = _route_stalled_by_policy(
            [reset_candidate],
            config=OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
            merged_ticket_ids=frozenset(),
            gh_blocked_ticket_ids=frozenset(),
        )

        assert routed == [reset_candidate]

    def test_main_drift_module_docstring_no_stale_929_citation(self) -> None:
        """main_drift.py's docstring must not attribute the counter to #929 (#974)."""
        import cw.reconcile.main_drift as main_drift_mod

        assert main_drift_mod.__doc__ is not None
        assert "#929" not in main_drift_mod.__doc__
        assert "consecutive_freshness_blocks" in main_drift_mod.__doc__


def test_stalled_cap_park_candidate_stamps_disposition(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """_detect_stalled_candidates's cap-exceeded PARK_BLOCKED_ON_USER carries
    paused_status (#976)."""
    from cw.reconcile import (
        _STALLED_CAP_PARKED_REASON,
        DEFAULT_STALLED_RETRY_CAP,
        ProposedAction,
        _detect_stalled_candidates,
    )

    worktree = tmp_path / "wt-cap-disp"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("cap-disp-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="cap-disp-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="cap-disp-1",
        stage=Stage.IMPL,
        attempts=DEFAULT_STALLED_RETRY_CAP,
    )

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(headless_timeout_by_stage={}),
        task_by_ticket={"cap-disp-1": task},
    )

    park = next(
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    )
    assert park.paused_status == _STALLED_CAP_PARKED_REASON


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


class TestActOnPhantomCandidatesSignalOnly:
    """Under signal_only, clean CRASH_COMPLETE phantoms → BLOCKED_ON_USER."""

    def test_signal_only_routes_clean_crash_to_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal_only: clean CRASH_COMPLETE → BLOCKED_ON_USER, not PENDING."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("so-phantom-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/so-phantom-1"
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="so-phantom-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="so-phantom-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="so-phantom-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="so-phantom-1",
            worktree_dirty=False,
            client="client-a",
        )

        reverted, _names, _usage, _salvaged, _results, _ = _act_on_phantom_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        # Return list contains only PENDING-routed; BLOCKED_ON_USER excluded
        assert "so-phantom-1" not in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "so-phantom-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == ReapReason.PHANTOM_SURFACE.value

    def test_signal_only_dirty_worktree_still_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dirty-worktree CRASH_COMPLETE: always BLOCKED_ON_USER (both policies)."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("dirty-phantom-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/dirty-phantom-1"
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="dirty-phantom-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="dirty-phantom-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="dirty-phantom-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="dirty-phantom-1",
            worktree_dirty=True,
            client="client-a",
        )

        # Both signal_only and auto produce BLOCKED_ON_USER for dirty-worktree
        reverted, _names, _usage, _salvaged, _results, _ = _act_on_phantom_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        assert "dirty-phantom-1" not in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "dirty-phantom-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_auto_policy_phantom_reverts_to_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AUTO policy: clean CRASH_COMPLETE → PENDING (regression guard)."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("auto-phantom-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/auto-phantom-1"
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="auto-phantom-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="auto-phantom-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="auto-phantom-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="auto-phantom-1",
            worktree_dirty=False,
            client="client-a",
        )

        reverted, _names, _usage, _salvaged, _results, _ = _act_on_phantom_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert "auto-phantom-1" in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "auto-phantom-1")
        assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# SESSION_REAP_PROPOSED emission tests (GitHub #555)
# ---------------------------------------------------------------------------


class TestEmitReapProposed:
    """_emit_reap_proposed emits SESSION_REAP_PROPOSED and stamps reap_proposed_at."""

    def test_reap_proposed_emits_event_for_revert_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """REVERT_TASK candidate → SESSION_REAP_PROPOSED event emitted, stamped."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-revert"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-revert-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-revert-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-revert-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        # Event was emitted
        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        ev = reap_events[0]
        assert ev.payload["session_id"] == "prop-revert-1"
        assert ev.payload["proposed_action"] == "revert_task"
        assert ev.payload["reason"] == "wall_clock_budget"
        assert ev.correlation_id == "prop-revert-1"

        # Session stamped
        assert sess.reap_proposed_at == now

    def test_reap_proposed_emits_event_for_crash_complete(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """CRASH_COMPLETE candidate → SESSION_REAP_PROPOSED event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)

        sess = _mk_session("prop-crash-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/prop-crash-1"
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-crash-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="prop-crash-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["proposed_action"] == "crash_complete"
        assert sess.reap_proposed_at == now

    def test_reap_proposed_emits_for_park_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """PARK_BLOCKED_ON_USER candidate → SESSION_REAP_PROPOSED event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("prop-park-1", "live-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-park-1",
            proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
            ticket_id="prop-park-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["proposed_action"] == "park_blocked_on_user"

    def test_reap_proposed_skips_non_reap_actions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """INCREMENT_COUNTER / SKIP_PARKED candidates → no event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)

        sess = _mk_session("prop-skip-1", "live-ref")
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))
        snap = _state_queue_snapshot()

        candidate = ReapCandidate(
            session_id="prop-skip-1",
            proposed_action=ProposedAction.INCREMENT_COUNTER,
            ticket_id="prop-skip-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        assert _state_queue_snapshot() == snap
        assert sess.reap_proposed_at is None

    def test_reap_proposed_dedup_skips_already_stamped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Session already stamped with reap_proposed_at → no duplicate event."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-dedup"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        first_stamp = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-dedup-1", worktree, started_at)
        sess.reap_proposed_at = first_stamp  # pre-stamped
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-dedup-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-dedup-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        # No event emitted (already proposed)
        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert reap_events == []
        # Stamp NOT overwritten
        assert sess.reap_proposed_at == first_stamp

    def test_reap_proposed_in_roster_sets_in_roster_true(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When surface_ref is in native_live, evidence.in_roster=True."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-roster"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-roster-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-roster-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-roster-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(
            state,
            [candidate],
            native_live={"fake-short-id"},  # matches sess.surface_ref
            now=now,
        )

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["evidence"]["in_roster"] is True

    def test_reap_proposed_not_in_roster_sets_in_roster_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When surface_ref not in native_live, evidence.in_roster=False."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-norost"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-norost-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-norost-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-norost-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["evidence"]["in_roster"] is False

    def test_reap_proposed_empty_candidates_no_writes(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Empty candidates list → no events, no state writes."""
        from cw.reconcile import _emit_reap_proposed

        worktree = tmp_path / "wt-prop-empty"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-empty-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))
        snap = _state_queue_snapshot()

        _emit_reap_proposed(state, [], native_live=set(), now=now)

        assert _state_queue_snapshot() == snap


# ---------------------------------------------------------------------------
# Per-lane reap_policy tests (GitHub #560)
# ---------------------------------------------------------------------------


class TestResolveReapPolicy:
    """resolve_reap_policy respects lane → global → SIGNAL_ONLY precedence."""

    def test_lane_policy_beats_global(self) -> None:
        """Lane AUTO overrides global SIGNAL_ONLY."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients = {
            "client-a": _client_with_lane("client-a", "fast", ReapPolicy.AUTO),
        }
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY)
        candidate = ReapCandidate(
            session_id="s1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t1",
            lane="fast",
            client="client-a",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_global_used_when_lane_not_in_client(self) -> None:
        """Lane name absent from client's lanes → falls back to global."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients = {
            "client-a": _client_with_lane("client-a", "slow", ReapPolicy.SIGNAL_ONLY),
        }
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s2",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t2",
            lane="fast",  # not in client's lanes
            client="client-a",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_global_used_when_client_not_in_dict(self) -> None:
        """Unknown client → falls back to global."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients: dict[str, ClientConfig] = {}
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s3",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t3",
            lane="fast",
            client="unknown-client",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_signal_only_failsafe_when_no_client(self) -> None:
        """candidate.client=None + global defaults → SIGNAL_ONLY."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients: dict[str, ClientConfig] = {}
        cfg = OrchestratorConfig()  # default: SIGNAL_ONLY
        candidate = ReapCandidate(
            session_id="s4",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t4",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.SIGNAL_ONLY

    def test_default_lane_resolves_via_global(self) -> None:
        """DEFAULT_LANE not in custom lanes → global policy used."""
        from cw.models import DEFAULT_LANE
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        lane_cfg = _client_with_lane("client-a", "custom-lane", ReapPolicy.SIGNAL_ONLY)
        clients = {"client-a": lane_cfg}
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s5",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t5",
            lane=DEFAULT_LANE,
            client="client-a",
        )
        # "default" lane not in client's declared lanes → global AUTO
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO


class TestActOnStalledCandidatesPerLane:
    """Per-lane reap_policy overrides global for stalled candidates."""

    def test_lane_auto_global_signal_acts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane AUTO + global SIGNAL_ONLY: REVERT_TASK candidate routed to PENDING."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-lane-auto-stalled"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

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
        _fast_client = _client_with_lane(
            "client-a", "fast", ReapPolicy.AUTO, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _fast_client},
        )

        sess = _mk_headless_daemon_session("lane-auto-stall-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-auto-stall-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-auto-stall-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-auto-stall-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-auto-stall-1",
            elapsed_seconds=3700.0,
            lane="fast",
            client="client-a",
        )

        # Global is SIGNAL_ONLY but lane is AUTO → should ACT (reverts to PENDING)
        reverted, _ = _act_on_stalled_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        assert "lane-auto-stall-1" in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-auto-stall-1")
        assert t.status == QueueItemStatus.PENDING

    def test_lane_signal_global_auto_signals(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane SIGNAL_ONLY + global AUTO: REVERT_TASK candidate → BLOCKED_ON_USER."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-lane-sig-stalled"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        _slow_client = _client_with_lane(
            "client-a", "slow", ReapPolicy.SIGNAL_ONLY, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _slow_client},
        )

        sess = _mk_headless_daemon_session("lane-sig-stall-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-sig-stall-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-sig-stall-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-sig-stall-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-sig-stall-1",
            elapsed_seconds=3700.0,
            lane="slow",
            client="client-a",
        )

        # Global is AUTO but lane is SIGNAL_ONLY → routes to BLOCKED_ON_USER
        reverted, _ = _act_on_stalled_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert reverted == []
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-sig-stall-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER


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


class TestActOnPhantomCandidatesPerLane:
    """Per-lane reap_policy overrides global for phantom candidates."""

    def test_lane_auto_global_signal_phantom_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane AUTO + global SIGNAL_ONLY: clean CRASH_COMPLETE → PENDING."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        _fast_ph_client = _client_with_lane(
            "client-a", "fast", ReapPolicy.AUTO, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _fast_ph_client},
        )

        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("lane-auto-ph-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/lane-auto-ph-1"
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-auto-ph-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-auto-ph-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-auto-ph-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="lane-auto-ph-1",
            worktree_dirty=False,
            client="client-a",
            lane="fast",
        )

        reverted, _names, _usage, _salvaged, _results, _ = _act_on_phantom_candidates(
            state, [candidate], now=now, config=OrchestratorConfig()
        )

        assert "lane-auto-ph-1" in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-auto-ph-1")
        assert t.status == QueueItemStatus.PENDING

    def test_lane_signal_global_auto_phantom_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane SIGNAL_ONLY + global AUTO: clean CRASH_COMPLETE → BLOCKED_ON_USER."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        _slow_ph_client = _client_with_lane(
            "client-a", "slow", ReapPolicy.SIGNAL_ONLY, workspace_path=tmp_path / "ws"
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": _slow_ph_client},
        )

        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("lane-sig-ph-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/lane-sig-ph-1"
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="lane-sig-ph-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="lane-sig-ph-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="lane-sig-ph-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="lane-sig-ph-1",
            worktree_dirty=False,
            client="client-a",
            lane="slow",
        )

        reverted, _names, _usage, _salvaged, _results, _ = _act_on_phantom_candidates(
            state, [candidate], now=now, config=_auto_config()
        )

        assert "lane-sig-ph-1" not in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "lane-sig-ph-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER


class TestMixedLanePolicySingleTick:
    """Single reconcile tick with two candidates on different lane policies."""

    def test_mixed_policy_stalled_one_acts_one_signals(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane A (auto) acts while lane B (signal_only) routes BLOCKED_ON_USER."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree_a = tmp_path / "wt-mixed-a"
        worktree_b = tmp_path / "wt-mixed-b"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

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

        from cw.models import LaneConfig

        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws",
            lanes=[
                LaneConfig(name="fast", reap_policy=ReapPolicy.AUTO),
                LaneConfig(name="slow", reap_policy=ReapPolicy.SIGNAL_ONLY),
            ],
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": client},
        )

        sess_a = _mk_headless_daemon_session("mixed-auto-1", worktree_a, started_at)
        sess_b = _mk_headless_daemon_session("mixed-sig-1", worktree_b, started_at)
        state = CwState(sessions=[sess_a, sess_b])
        save_state(state)
        task_a = TicketTask(
            ticket_id="mixed-auto-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="mixed-auto-1",
        )
        task_b = TicketTask(
            ticket_id="mixed-sig-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="mixed-sig-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))

        candidate_a = ReapCandidate(
            session_id="mixed-auto-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="mixed-auto-1",
            elapsed_seconds=3700.0,
            lane="fast",
            client="client-a",
        )
        candidate_b = ReapCandidate(
            session_id="mixed-sig-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="mixed-sig-1",
            elapsed_seconds=3700.0,
            lane="slow",
            client="client-a",
        )

        # Global is SIGNAL_ONLY; lane A is AUTO, lane B is SIGNAL_ONLY
        reverted, _ = _act_on_stalled_candidates(
            state,
            [candidate_a, candidate_b],
            now=now,
            config=OrchestratorConfig(),
        )

        # Lane A (fast/AUTO): reverted to PENDING
        assert "mixed-auto-1" in reverted
        # Lane B (slow/SIGNAL_ONLY): routes to BLOCKED_ON_USER
        assert "mixed-sig-1" not in reverted

        store = load_dev_queue()
        t_a = next(t for t in store.tasks if t.ticket_id == "mixed-auto-1")
        t_b = next(t for t in store.tasks if t.ticket_id == "mixed-sig-1")
        assert t_a.status == QueueItemStatus.PENDING
        assert t_b.status == QueueItemStatus.BLOCKED_ON_USER


class TestReapProposedPayloadLane:
    """SESSION_REAP_PROPOSED payload includes lane field (GitHub #560)."""

    def test_reap_proposed_payload_includes_lane(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Candidate with lane='fast' → payload has lane='fast'."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-lane-payload"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("lane-payload-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="lane-payload-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-payload-1",
            elapsed_seconds=3700.0,
            lane="fast",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["lane"] == "fast"

    def test_reap_proposed_default_lane_in_payload(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Candidate with default lane → payload has lane='default'."""
        from cw.models import DEFAULT_LANE
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-default-lane-payload"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("default-lane-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="default-lane-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="default-lane-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["lane"] == DEFAULT_LANE


# ---------------------------------------------------------------------------
# GitHub #578 — ROUTE_EMITTED_SENTINEL: reconcile honors emitted-but-unrouted
# sentinels without waiting for signal_stop
# ---------------------------------------------------------------------------


class TestRouteEmittedSentinel:
    """flag_silently_idle_daemon_sessions routes a transcript sentinel before
    the idle watchdog budget fires (GitHub #578)."""

    def test_detect_sentinel_present_routes_before_watchdog(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel present + last_result None + elapsed >= 300 s
        → ROUTE_EMITTED_SENTINEL, task COMPLETED, session COMPLETED."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-detect"
        # 305 s elapsed — past the 300-s check but well under 900-s watchdog.
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)
        assert (now - started_at).total_seconds() >= 300
        assert (now - started_at).total_seconds() < IDLE_WATCHDOG_SECONDS

        sess = _mk_headless_daemon_session("578-detect", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-1", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        # B2: apply_staged_decision needs a pipeline to decide COMPLETED vs advance.
        # Ship at FINALIZE (terminal) → COMPLETED; must have clients.yaml on disk.
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-detect",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-detect",
                        stage=Stage.FINALIZE,  # terminal; shipped here → COMPLETED
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-detect")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "shipped"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-detect")
        assert task.status == QueueItemStatus.COMPLETED

        mock_daemon.stop.assert_called_once_with("fake-short-id")

    def test_detect_elapsed_under_threshold_no_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """elapsed < sentinel_unrouted_check_seconds → no ROUTE_EMITTED_SENTINEL
        candidate, sentinel left in transcript for next tick."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-under"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 4, 0, tzinfo=UTC)  # 240 s < 300 s threshold
        assert (now - started_at).total_seconds() < 300

        sess = _mk_headless_daemon_session("578-under", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-2", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-under",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-under",
                    )
                ]
            )
        )

        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-under")
        # Session must NOT be completed — too early for the sentinel check.
        assert reloaded.status == SessionStatus.ACTIVE

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-under")
        assert task.status == QueueItemStatus.RUNNING

    def test_detect_last_result_set_skips_double_route(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session.last_result already set → ROUTE_EMITTED_SENTINEL skipped
        (signal_stop already ran; prevents double-routing)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-dbl"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-dbl", worktree, started_at)
        # Simulate: signal_stop already ran and stored last_result.
        sess.last_result = {"status": "shipped"}
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-3", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-dbl",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-dbl",
                    )
                ]
            )
        )

        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

        assert blocked == []
        # Session stays ACTIVE — watchdog budget not yet exhausted, no route.
        reloaded = next(s for s in load_state().sessions if s.id == "578-dbl")
        assert reloaded.status == SessionStatus.ACTIVE

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-dbl")
        assert task.status == QueueItemStatus.RUNNING

    def test_detect_live_session_with_sentinel_routes_not_crash_complete(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live-roster session + sentinel → ROUTE_EMITTED_SENTINEL, NOT a crash path.
        Session ends COMPLETED/NORMAL (not TIMED_OUT)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-live"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-live", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-4", _no_op_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-live",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-live",
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-live")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "no_op"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-live")
        assert task.status == QueueItemStatus.COMPLETED

    def test_act_blocked_retry_eligible_routes_to_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel status=blocked + retry_eligible=True → task reverts to PENDING."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-retry"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-retry", worktree, started_at)
        sess.last_result = None

        blocked_retry_payload: dict[str, Any] = {
            "schema_version": 4,
            "ticket_id": "578-retry",
            "status": "blocked",
            "stage_reached": "stage2_impl",
            "scope": {
                "tier": "small",
                "files": 0,
                "lines_estimate": 0,
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
                "lowest_agent_confidence": "LOW",
                "any_incomplete_risk": True,
                "shortcuts": [],
                "recommendation": "EXIT_FOR_HUMAN_REVIEW",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": {
                "stage": "stage2_impl",
                "reason": "impl_failed",
                "retry_eligible": True,
                "details": "agent timed out",
                "next_actions": ["redispatch_ticket"],
            },
            "next_actions": ["redispatch_ticket"],
        }
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-5", blocked_retry_payload
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-retry",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-retry",
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-retry")
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

    def test_act_signal_only_does_not_gate_route_emitted_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal_only policy does NOT block ROUTE_EMITTED_SENTINEL — routing an
        emitted sentinel is constructive, not a destructive reap."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-sigonly"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-sigonly", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-6", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        # B2: apply_staged_decision needs a pipeline to decide COMPLETED vs advance.
        # Ship at FINALIZE (terminal) → COMPLETED; must have clients.yaml on disk.
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-sigonly",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-sigonly",
                        stage=Stage.FINALIZE,  # terminal; shipped here → COMPLETED
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            # signal_only policy — normally gates reaps.
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-sigonly")
        # ROUTE_EMITTED_SENTINEL is exempt from signal_only → session COMPLETED.
        assert reloaded.status == SessionStatus.COMPLETED
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-sigonly")
        assert task.status == QueueItemStatus.COMPLETED

    def test_act_session_completed_event_emitted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ROUTE_EMITTED_SENTINEL emits a SESSION_COMPLETED event."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-event"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-event", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-7", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-event",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-event",
                        # Matches _shipped_salvage_payload()'s stage_reached
                        # ("stage5_post_create") so the #1019/#1031
                        # stage-match guard accepts the route.
                        stage=Stage.FINALIZE,
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        events = read_events(
            consumer="test-578-event",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] == "578-event"
        assert payload["status"] == "shipped"
        assert payload["salvaged"] is True
        assert payload["crashed"] is False

    def test_review_pending_approval_sentinel_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: review_pending_approval-shaped sentinel emitted without
        signal_stop routes task to BLOCKED_ON_USER (PAUSED_FOR_USER_INPUT path),
        not stuck as RUNNING indefinitely. See #633."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-rpa"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        ticket_id = "578-rpa"
        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        sess.last_result = None
        payload = _make_terminal_payload("review_pending_approval", ticket_id)
        _write_salvage_transcript(home, worktree, "claude-578-uuid-8", payload)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id=ticket_id,
                        # stage_reached=stage3_review in the payload above must
                        # match the seeded task's stage -- otherwise #1019's
                        # stage-mismatch guard refuses to route it (a task at
                        # the default Stage.PLAN could never realistically
                        # emit a review-stage sentinel).
                        stage=Stage.REVIEW,
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "review_pending_approval"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        # review_pending_approval is in PAUSED_FOR_USER_INPUT_STATUSES (#633).
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_route_emitted_sentinel_refusal_stops_refiring(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1149: a stage-mismatch refusal stamps a paused_status-only
        marker on session.last_result AND persists it, so the doomed candidate
        is not re-proposed on a subsequent tick (extends #1031's non-orphan
        guard with the actual anti-refiring fix).

        Same shape as ``test_idle_stage_mismatch_does_not_orphan_task_or_
        complete_session`` (task row already advanced past the sentinel's
        stage_reached), but drives the full ``flag_silently_idle_daemon_
        sessions`` act phase end-to-end, then reloads from disk to prove the
        marker survived. This exercises the #1149 save-gate fix: on a
        pure-refusal tick the ``accepted`` routed-sentinel list is empty, so
        ``has_dispositions`` is False; without ``_apply_idle_routed_mutations``
        signalling that it mutated session state, ``_act_on_idle_candidates``
        would skip ``save_state`` and lose the marker, and the candidate would
        re-fire on the next (fresh ``load_state``) tick forever.
        """
        from cw.reconcile import ProposedAction, _detect_idle_candidates

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-refusal"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-refusal", worktree, started_at)
        sess.last_result = None  # sentinel NOT yet consumed -> ROUTE_EMITTED eligible
        payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
        payload["ticket_id"] = "1149-refusal"
        _write_salvage_transcript(home, worktree, "claude-1149-refusal", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-refusal",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-refusal",
            # Row already advanced past IMPL by the time this stale IMPL-leg
            # sentinel is discovered -- the #986 shape.
            stage=Stage.REVIEW,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
                task_by_ticket={"1149-refusal": task},
            )

        # Refusal must not complete the still-alive surface, and the marker must
        # be PERSISTED (the #1149 save-gate fix) so the next fresh-load tick sees
        # it. Read back from disk, not the in-memory object.
        mock_daemon.stop.assert_not_called()
        reloaded_state = load_state()
        reloaded = next(s for s in reloaded_state.sessions if s.id == "1149-refusal")
        assert reloaded.status != SessionStatus.COMPLETED
        assert reloaded.last_result == {
            "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }
        task_after = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "1149-refusal"
        )
        assert task_after.stage == Stage.REVIEW
        assert task_after.status == QueueItemStatus.RUNNING

        # Second tick over the reloaded (marked) session: the marker flips
        # `last_result is None` false, so the ROUTE_EMITTED_SENTINEL candidate is
        # not re-proposed -- the refusal loop is broken.
        candidates_2 = _detect_idle_candidates(
            reloaded_state,
            now=now + timedelta(seconds=1),
            native_live={"fake-short-id"},
            config=_auto_config(),
            task_by_ticket={"1149-refusal": task_after},
        )
        assert all(
            c.proposed_action != ProposedAction.ROUTE_EMITTED_SENTINEL
            for c in candidates_2
        )

    def test_route_emitted_sentinel_refusal_marker_is_not_terminal_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1149 refusal marker carries no "status" key, so
        _has_terminal_sentinel stays False -- the session is not mistaken for
        genuinely terminal and the ordinary idle-stall machinery still runs."""
        from cw.reconcile import _detect_idle_candidates
        from cw.reconcile.idle import _apply_idle_routed_mutations

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-not-terminal"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-not-terminal", worktree, started_at)
        sess.last_result = None
        payload = _stage_complete_payload()
        payload["ticket_id"] = "1149-not-terminal"
        _write_salvage_transcript(home, worktree, "claude-1149-nt", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-not-terminal",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-not-terminal",
            stage=Stage.REVIEW,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
            task_by_ticket={"1149-not-terminal": task},
        )
        session_by_id = {s.id: s for s in state.sessions}
        _apply_idle_routed_mutations(session_by_id, candidates, now=now)

        reloaded = session_by_id["1149-not-terminal"]
        assert reloaded.last_result == {
            "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }
        assert _has_terminal_sentinel(reloaded) is False

    def test_idle_later_stage_sentinel_routes_forward_instead_of_looping(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1149 R1, idle.py wiring: a later-stage sentinel (a legitimate
        self-escalation the row hasn't caught up to) routes forward via the
        shared staged-advance authority through the full alive-idle sweep
        (flag_silently_idle_daemon_sessions -> _detect_idle_candidates ->
        _apply_idle_routed_mutations) -- it does NOT hit the #1149 R4 refusal
        branch, so no paused_status marker is stamped and the session
        completes normally. Mirrors
        test_phantom_later_stage_sentinel_routes_forward_instead_of_looping's
        coverage for the idle-sweep call path, which was previously untested.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-idle-later"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-idle-later", worktree, started_at)
        sess.last_result = None  # sentinel NOT yet consumed -> ROUTE_EMITTED eligible
        payload = _stage_complete_payload()
        payload["ticket_id"] = "1149-idle-later"
        payload["status"] = "blocked"
        payload["stage_reached"] = "stage4b_pr_create"  # FINALIZE
        payload["blocker"] = {
            "stage": "stage4b_pr_create",
            "reason": "merge_gate_failed",
            "retry_eligible": False,
            "details": "self-escalated",
            "next_actions": [],
        }
        _write_salvage_transcript(home, worktree, "claude-1149-idle-later", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-idle-later",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-idle-later",
            # IMPL is EARLIER than the sentinel's mapped FINALIZE stage ->
            # "later" position: a legitimate self-escalation, walked forward.
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
                task_by_ticket={"1149-idle-later": task},
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "1149-idle-later")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result.get("paused_status") is None
        mock_daemon.stop.assert_called_once_with("fake-short-id")

        task_after = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "1149-idle-later"
        )
        # Walk IMPL -> REVIEW -> FINALIZE, then Rule 5 routes the blocked
        # status at the landed FINALIZE stage.
        assert task_after.stage == Stage.FINALIZE
        assert task_after.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# GitHub #637 — world-state check before revert (merged-PR guard)
# ---------------------------------------------------------------------------


class TestWorldStateCheckBeforeRevert:
    """_act_on_stalled/idle/phantom skip revert when PR is already merged."""

    # --- stalled ---

    def test_stalled_merged_ticket_completes_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED session + COMPLETED queue, not TIMED_OUT."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-stalled-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("stalled-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="stalled-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="stalled-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="stalled-merged-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="stalled-merged-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        reverted, merged = _act_on_stalled_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            merged_ticket_ids=frozenset({"stalled-merged-1"}),
        )

        assert reverted == []
        assert "stalled-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "stalled-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-stalled-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("crashed") is False

    def test_stalled_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → BLOCKED_ON_USER queue task, NEEDS_ATTENTION."""
        from cw.reconcile import (
            _GH_CHECK_BLOCKED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-stalled-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("stalled-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="stalled-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="stalled-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="stalled-ghblock-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="stalled-ghblock-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
            lane="stalled-ghblock-lane",
        )

        reverted, merged = _act_on_stalled_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            gh_blocked_ticket_ids=frozenset({"stalled-ghblock-1"}),
        )

        assert reverted == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "stalled-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == _GH_CHECK_BLOCKED_REASON
        assert sess.status == SessionStatus.TIMED_OUT

        events = read_events(
            consumer="test-stalled-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "stalled-ghblock-lane"

    # --- idle ---

    def test_idle_merged_ticket_completes_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED session + COMPLETED queue, not TIMED_OUT."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        worktree = tmp_path / "wt-idle-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("idle-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="idle-merged-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="idle-merged-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.IDLE_STALL,
            client="client-a",
        )

        blocked, merged, _salvage = _act_on_idle_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset({("client-a", "idle-merged-1")}),
        )

        assert blocked == []
        assert "idle-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-idle-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("crashed") is False

    def test_detect_idle_candidates_merged_finalize_completes_not_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Merged FINALIZE-stage worktree session (Mode A) completes shipped,
        not SALVAGE_GIT / needs_salvage (#1054)."""
        from cw.reconcile import _act_on_idle_candidates, _detect_idle_candidates

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        wt_path = tmp_path / "wt-idle-merged-finalize"
        wt_path.mkdir(parents=True)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "auto-dev/idle-merged-finalize-1",
        )

        sess = _mk_live_idle_daemon_session(
            "idle-merged-finalize-1",
            "live-ref",
            started_at,
            idle_observation_count=1,
            worktree_path=wt_path,
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-merged-finalize-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-merged-finalize-1",
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=2),
            task_by_ticket={"idle-merged-finalize-1": task},
            merged_client_ticket_ids=frozenset(
                {("client-a", "idle-merged-finalize-1")}
            ),
        )

        blocked, merged, salvage_git = _act_on_idle_candidates(
            state,
            candidates,
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset(
                {("client-a", "idle-merged-finalize-1")}
            ),
        )

        assert blocked == []
        assert salvage_git == []
        assert "idle-merged-finalize-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-merged-finalize-1")
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "shipped"

    def test_idle_merged_finalize_does_not_complete_different_clients_same_ticket(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full pipeline regression (#1054): client-a's merged FINALIZE session
        completes shipped, but a DIFFERENT client's RUNNING task sharing the
        same ticket_id string must NOT be swept into COMPLETED by the
        merged-first candidate's downstream act phase (_act_on_idle_candidates'
        merge split + _apply_idle_queue_mutations' dev-queue sweep both key on
        bare ticket_id pre-#1054; this proves they are now (client, ticket_id)
        scoped end to end, not just at the classify entry point)."""
        from cw.reconcile import _act_on_idle_candidates, _detect_idle_candidates

        ticket_id = "collide-finalize-1"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        wt_path = tmp_path / "wt-idle-collide-finalize"
        wt_path.mkdir(parents=True)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "auto-dev/collide-finalize-1",
        )

        sess = _mk_live_idle_daemon_session(
            "collide-finalize-1",
            "live-ref",
            started_at,
            idle_observation_count=1,
            worktree_path=wt_path,
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task_a = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="collide-finalize-1",
            stage=Stage.FINALIZE,
        )
        # client-b's unrelated, unmerged task happens to share the ticket_id
        # string -- must survive this tick untouched.
        task_b = TicketTask(
            ticket_id=ticket_id,
            client="client-b",
            status=QueueItemStatus.RUNNING,
            session_id="some-other-live-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=2),
            task_by_ticket={ticket_id: task_a},
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)}),
        )

        blocked, merged, salvage_git = _act_on_idle_candidates(
            state,
            candidates,
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)}),
        )

        assert blocked == []
        assert salvage_git == []
        assert ticket_id in merged
        assert sess.status == SessionStatus.COMPLETED

        store = load_dev_queue()
        reloaded_a = next(
            t
            for t in store.tasks
            if t.client == "client-a" and t.ticket_id == ticket_id
        )
        reloaded_b = next(
            t
            for t in store.tasks
            if t.client == "client-b" and t.ticket_id == ticket_id
        )
        assert reloaded_a.status == QueueItemStatus.COMPLETED
        assert reloaded_a.disposition == "shipped"
        # The regression this test guards against: pre-fix, this would also
        # read COMPLETED because _apply_idle_queue_mutations matched on bare
        # ticket_id with no client filter.
        assert reloaded_b.status == QueueItemStatus.RUNNING

    def test_idle_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → BLOCKED_ON_USER queue task, NEEDS_ATTENTION."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        worktree = tmp_path / "wt-idle-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("idle-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="idle-ghblock-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="idle-ghblock-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.IDLE_STALL,
            lane="idle-ghblock-lane",
        )

        blocked, merged, _salvage = _act_on_idle_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            gh_blocked_ticket_ids=frozenset({"idle-ghblock-1"}),
        )

        assert blocked == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert sess.status == SessionStatus.TIMED_OUT

        events = read_events(
            consumer="test-idle-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "idle-ghblock-lane"

    # --- phantom ---

    def test_phantom_merged_ticket_completes_not_crashes(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED+NORMAL phantom, not COMPLETED+CRASHED."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-merged-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-merged-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-merged-1",
            worktree_dirty=False,
            client="client-a",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                merged_ticket_ids=frozenset({"phantom-merged-1"}),
            )
        )

        assert reverted == []
        assert "phantom-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        completed = [e for e in events if not e.payload.get("crashed")]
        assert len(completed) == 1

    def test_phantom_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → phantom skipped, BLOCKED_ON_USER."""
        from cw.reconcile import (
            _GH_CHECK_BLOCKED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-ghblock-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-ghblock-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-ghblock-1",
            worktree_dirty=False,
            client="client-a",
            lane="phantom-lane",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                gh_blocked_ticket_ids=frozenset({"phantom-ghblock-1"}),
            )
        )

        assert reverted == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == _GH_CHECK_BLOCKED_REASON
        assert sess.status == SessionStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "phantom-lane"

    # --- reconcile() pre-pass ---

    def test_reconcile_prepass_merged_pr_populates_completed_ticket_ids(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: merged PR → completed_ticket_ids, task COMPLETED."""
        worktree = tmp_path / "wt-reconcile-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            list,
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-merged-1" in report.completed_ticket_ids
        assert "reconcile-merged-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

    def test_reconcile_prepass_gh_unavailable_blocks_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: gh unavailable → task BLOCKED_ON_USER, not PENDING."""
        worktree = tmp_path / "wt-reconcile-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, False),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            list,
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-ghblock-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_reconcile_prepass_custom_prefix(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass passes branch='feat/<ticket_id>' when prefix='feat'."""
        ticket_id = "reconcile-feat-1"
        worktree = tmp_path / "wt-reconcile-feat"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # Write clients.yaml with feature_branch_prefix: feat
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"feat/{ticket_id}"]

    def test_reconcile_prepass_passes_cwd(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass resolves cwd from client's workspace_path (#1269)."""
        ticket_id = "reconcile-cwd-1"
        worktree = tmp_path / "wt-reconcile-cwd"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_cwd: list[Path | None] = []

        def _capture(
            tid: str, *, branch: str, cwd: Path | None = None, **_kw: object
        ) -> tuple[bool, bool]:
            captured_cwd.append(cwd)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_cwd == [Path("/tmp/ws-feat")]

    def test_reconcile_prepass_default_prefix_fallback(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: no clients.yaml → branch='dev/<ticket>' (default)."""
        ticket_id = "reconcile-default-1"
        worktree = tmp_path / "wt-reconcile-default"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # No clients.yaml written — load_clients() returns {}

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"dev/{ticket_id}"]

    def test_reconcile_prepass_dangling_client_skips_gh_call(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: clients.yaml populated but missing this
        session's client → skip the gh call entirely (GitHub #1269).

        Distinct from "no clients.yaml at all" (see
        test_reconcile_prepass_default_prefix_fallback, which must still
        call gh with cwd=None): here clients.yaml is populated with a
        *different* client, so this session's client is dangling/config-
        drifted. An unscoped gh call would risk the same cross-repo
        misattribution the ticket describes.
        """
        ticket_id = "reconcile-dangling-1"
        worktree = tmp_path / "wt-reconcile-dangling"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            calls.append(tid)
            return True, True  # would falsely report merged if ever called

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert calls == []

    def test_stalled_dead_session_wrong_repo_merge_does_not_phantom_reap(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dead stalled session's ticket must not be phantom-reaped as
        merged/shipped just because the gh CLI's ambient CWD (not scoped to
        the client's repo) happens to answer a same-numbered ticket in a
        different repo as merged. Real repro of GitHub #1269: fakes
        ``cw.gh._sp.run`` itself (not ``_deps.pr_is_merged_for_ticket``) so
        the exact code path under test — cwd threading through
        ``pr_is_merged_for_ticket`` — is exercised end to end via the real
        ``reconcile()`` entry point.

        Pre-fix: no cwd is ever passed to the gh subprocess, so every call
        lands in the "ambient" branch below, which reports the ticket
        MERGED — reproducing the incident (task COMPLETED/"shipped",
        SESSION_COMPLETED with reason="phantom_reap_merged").

        Post-fix: the pre-pass and stalled sweep resolve cwd from the
        client's ``workspace_path`` via ``_git_dir``, so the gh calls land
        in the "scoped correctly" branch, which reports the ticket
        genuinely unmerged in client A's own repo — routing the task to
        BLOCKED_ON_USER under the default SIGNAL_ONLY reap policy instead.
        """
        ticket_id = "wrongrepo-1"
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        worktree = tmp_path / "wt-wrongrepo"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            f"    workspace_path: {repo_a}\n"
            "    default_branch: main\n"
        )

        def _fake_run(args: list[str], **kwargs: object) -> Any:
            cwd = kwargs.get("cwd")
            scoped_correctly = cwd == repo_a
            result = MagicMock()
            result.returncode = 0
            if "issue" in args:
                # Correctly scoped to client A's repo: ticket has no linked
                # PRs there (genuinely unmerged). Ambient/unscoped: simulate
                # the collision — a same-numbered ticket that IS merged in
                # whatever repo the ambient CWD happens to resolve to.
                refs = [] if scoped_correctly else [{"number": 999}]
                result.stdout = json.dumps({"closedByPullRequestsReferences": refs})
            elif "list" in args:
                result.stdout = json.dumps([])
            else:
                result.stdout = json.dumps(
                    {"state": "OPEN" if scoped_correctly else "MERGED"}
                )
            return result

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.status != QueueItemStatus.COMPLETED
        assert t.disposition != "shipped"

        events = read_events(
            consumer=f"test-{ticket_id}-phantom-reap-guard",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("reason") == "phantom_reap_merged" for e in events)


# ---------------------------------------------------------------------------
# _apply_sentinel_to_task — staged advance tests (GitHub issue #698)
# Regression coverage for the reconcile path that was unreachable in B2:
# a plan_pending_approval sentinel with small scope must advance PLAN→IMPL
# via _apply_sentinel_to_task, not fall through to BLOCKED_ON_USER via the
# stale monolith mapping.
# ---------------------------------------------------------------------------


def _plan_pending_approval_sentinel(
    ticket_id: str, scope_tier: str | None
) -> AutoDevResult:
    """Build a valid AutoDevResult for status=plan_pending_approval at stage1_plan.

    Reproduces the real #663 dogfood sentinel: PLAN stage exits before impl
    so lines_actual=None and scope.tier may be None (B2 pre-impl exempt rule).
    """
    return AutoDevResult.model_validate(
        {
            "schema_version": 4,
            "ticket_id": ticket_id,
            "status": "plan_pending_approval",
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": scope_tier,
                "files": 4,
                "lines_estimate": 120,
                "lines_actual": None,
                "forbidden_touched": False,
            },
            "plan_source": "github_issue_existing",
            "branch": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                # None allowed pre-impl (§3.3 scope.tier/confidence exemption)
                "lowest_agent_confidence": None,
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
            },
            "next_actions": ["user_approve_plan"],
        }
    )


class TestApplySentinelToTaskStagedAdvance:
    """Regression tests for GitHub #698: _apply_sentinel_to_task must use the
    staged advance decision (apply_staged_decision) rather than the stale
    monolith mapping that predates B2.
    """

    def test_plan_pending_approval_small_scope_advances_plan_to_impl(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier='small' → PENDING at IMPL stage.

        This is the exact production failure from #698: the monolith path
        routed this sentinel to BLOCKED_ON_USER; the staged path auto-advances.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-small"
        session_id = "sess-698-small"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier="small")
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING, (
            f"expected PENDING (staged advance), got {t.status!r} — "
            "monolith mapping was BLOCKED_ON_USER (#698)"
        )
        assert t.stage == Stage.IMPL, (
            f"expected stage=IMPL after PLAN→IMPL advance, got {t.stage!r}"
        )

    def test_status_unknown_blocked_does_not_complete_task(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """#750: an unknown-status sentinel must NOT be marked COMPLETED.

        A worker that emitted status='proceed' (not a valid Status) parses to a
        BlockedResult(status_unknown). The old `else → COMPLETED` fallback
        silently marked the ticket shipped despite no branch/PR (the #728 loss).
        It must route to FAILED — never claim false success.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)
        ticket_id = "GH-750"
        session_id = "sess-750"
        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            '{"schema_version": 4, "ticket_id": "GH-750", "status": "proceed"}\n'
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "status_unknown"

        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED, (
            f"unknown-status sentinel must not COMPLETE (silent false-ship); "
            f"got {t.status!r}"
        )

    def test_plan_pending_approval_null_tier_scope_hint_small_advances(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier=None + scope_hint='small' → IMPL.

        Reproduces the #663 dogfood shape: real PLAN sentinels emit tier=None
        (lines_actual unknown pre-impl). The #696 scope_hint fallback must
        reach production via the reconcile path, not just the consume path.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-null-tier"
        session_id = "sess-698-null-tier"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="small",  # fallback tier source per #696
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier=None)
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.IMPL

    def test_plan_pending_approval_large_scope_blocks(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier='large' → BLOCKED_ON_USER (gate)."""
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-large"
        session_id = "sess-698-large"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier="large")
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER


def _blocked_autodev_payload(ticket_id: str) -> dict[str, Any]:
    """Minimal valid AutoDevResult with status='blocked' at IMPL.

    Routes through STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER when applied.
    """
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": "blocked",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": 5,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
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
            "recommendation": "EXIT_FOR_HUMAN_REVIEW",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": {
            "stage": "stage2_impl",
            "reason": "agent_block",
            "details": "still failing",
        },
        "next_actions": [],
    }


class TestApplySentinelToTaskLateRescue:
    """GitHub #918: a late Stop-hook sentinel must rescue an idle-parked task.

    The idle watchdog can park a still-completing session BLOCKED_ON_USER
    (retaining session_id). When the late sentinel arrives, the widened
    _apply_sentinel_to_task lookup re-finds the parked task and routes it
    through the shared _route_staged_decision. Returns True iff a parked task
    was rescued via the AutoDevResult arm.
    """

    def _seed_parked_task(
        self,
        tmp_config_dir: Path,
        *,
        ticket_id: str,
        session_id: str,
        stage: Stage,
        scope_hint: str = "small",
        status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    ) -> None:
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=status,
            session_id=session_id,  # retained across the park (#918)
            stage=stage,
            scope_hint=scope_hint,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

    def test_late_stage_complete_rescues_parked_task_nonterminal(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked at IMPL + late stage_complete → PENDING at REVIEW; return True."""
        ticket_id, session_id = "GH-918-nonterm", "sess-918-nonterm"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_late_shipped_rescues_parked_task_terminal_preserves_pr_url(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked at FINALIZE + late shipped → COMPLETED + pr_url; return True."""
        ticket_id, session_id = "GH-918-ship", "sess-918-ship"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.FINALIZE,
        )
        sentinel = AutoDevResult.model_validate(_shipped_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "shipped"
        assert t.pr_url == "https://github.com/foo/bar/pull/99"

    def test_late_no_op_rescues_parked_task(self, tmp_config_dir: Path) -> None:
        """Parked + late no_op → COMPLETED disposition='no_op'; return True."""
        ticket_id, session_id = "GH-918-noop", "sess-918-noop"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.PLAN,
        )
        sentinel = AutoDevResult.model_validate(_no_op_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "no_op"

    def test_late_blocked_autodevresult_reparks_parked_task(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked + late AutoDevResult(blocked) → stays BLOCKED_ON_USER; return True.

        The #923 disposition re-stamp is allowed (Comment 2 rule 5) — rescue
        reports True because the AutoDevResult arm handled a parked task.
        """
        ticket_id, session_id = "GH-918-blk", "sess-918-blk"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_blocked_autodev_payload(ticket_id))

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == "blocked"

    def test_late_blocked_result_leaves_parked_task_untouched(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked + late BlockedResult → task untouched; return False (Comment 9).

        A malformed/unknown-status sentinel carries no success signal, so the
        parked task must not be mutated (no false FAILED, no false completion).
        """
        ticket_id, session_id = "GH-918-blkres", "sess-918-blkres"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            f'{{"schema_version": 4, "ticket_id": "{ticket_id}", '
            '"status": "proceed"}\n'
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "status_unknown"

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition is None

    def test_running_task_autodevresult_returns_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING task + stage_complete advances (existing #698) and returns False.

        Only a parked (BLOCKED_ON_USER) rescue reports True; the live RUNNING
        path returns False even though it advances.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-run", "sess-918-run"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_running_blocked_result_unchanged(self, tmp_config_dir: Path) -> None:
        """RUNNING task + validation_failed BlockedResult still routes as today.

        Regression guard on the widened lookup: the BlockedResult arms must run
        only for RUNNING and behave exactly as before (PENDING + clear session).
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-runblk", "sess-918-runblk"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            "{\n"
            '  "schema_version": 4,\n'
            f'  "ticket_id": "{ticket_id}",\n'
            '  "status": "shipped",\n'
            '  "stage_reached": "stage5_post_create",\n'
            '  "scope": {"tier": "small", "files": 1, "lines_estimate": 10, '
            '"lines_actual": 5, "forbidden_touched": false},\n'
            '  "plan_source": "github_issue_existing",\n'
            '  "branch": "auto-dev/918",\n'
            '  "worktree_path": null,\n'
            '  "fork_point_sha": "abc123",\n'
            '  "commits": ["def456"],\n'
            '  "pr": null,\n'
            '  "review": {"must_fix_initial": 0, "should_fix": 0, '
            '"fix_cycles_used": 0},\n'
            '  "health": {"lowest_agent_confidence": "HIGH", '
            '"any_incomplete_risk": false, "shortcuts": [], '
            '"recommendation": "PROCEED", "downgrade_applied": false, '
            '"fix_loop_escalated": false},\n'
            '  "friction_highlights": [],\n'
            '  "blocker": null,\n'
            '  "next_actions": ["wait_for_ci"]\n'
            "}\n"
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "validation_failed"

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None

    def test_running_no_result_emitted_requeues(self, tmp_config_dir: Path) -> None:
        """RUNNING task + transient no_result_emitted → PENDING, session cleared.

        Regression guard on the extracted _route_blocked_result_to_task helper:
        the transient parse-failure branch must still re-queue a RUNNING task.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-transient", "sess-918-transient"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = parse_stdout("narrative only, no sentinel block emitted\n")
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "no_result_emitted"

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None

    def test_late_sentinel_rescues_signoff_parked_task(
        self, tmp_config_dir: Path
    ) -> None:
        """A signoff-parked (AWAITING_OPERATOR_SIGNOFF) task is rescued by a late
        sentinel the same way a BLOCKED_ON_USER task is (#990).

        No signoff is configured on `_write_staged_clients_yaml`'s client, so
        this exercises only the widened membership lookup (touch-point #30)
        -- not the signoff gate re-firing.
        """
        ticket_id, session_id = "GH-990-signoff", "sess-990-signoff"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_late_sentinel_re_parks_signoff_ticket_at_review_idempotently(
        self, tmp_config_dir: Path
    ) -> None:
        """The realistic signoff-rescue case: a task parked AWAITING_OPERATOR_
        SIGNOFF at Stage.REVIEW (the only stage the gate ever fires at) with
        signoff still configured, re-entering via a late duplicate sentinel.

        Unlike ``test_late_sentinel_rescues_signoff_parked_task`` (which seeds
        an IMPL-stage state unreachable through any real gate-firing path, to
        isolate the widened membership lookup), this seeds the actual
        production state: the gate re-fires on re-entry through
        ``_route_staged_decision`` and re-parks the ticket at
        AWAITING_OPERATOR_SIGNOFF -- a true no-op, not an accidental advance
        to FINALIZE (#990).
        """
        ticket_id, session_id = "GH-990-signoff-review", "sess-990-signoff-review"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.REVIEW,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        store = load_dev_queue()
        store.tasks[0].signoff = "operator"
        save_dev_queue(store)
        # stage_reached must match the seeded Stage.REVIEW task -- the shared
        # _stage_complete_payload() fixture carries stage_reached=stage2_impl
        # (IMPL), which would now be a stage-mismatch refusal under #1019's
        # guard rather than exercising the #918 rescue arm's signoff re-park.
        payload = _stage_complete_payload()
        payload["stage_reached"] = "stage3_review"
        sentinel = AutoDevResult.model_validate(payload)

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert t.stage == Stage.REVIEW
        assert t.disposition == "signoff_gate"

    def test_late_sentinel_stage_mismatch_refuses_rescue_reports_not_rescued(
        self, tmp_config_dir: Path
    ) -> None:
        """A stale sentinel against a parked task refuses the #918 rescue (#1019).

        Parked at REVIEW; the late sentinel carries stage_reached=stage2_impl
        (a previous IMPL leg). The shared stage-mismatch guard refuses to route
        it -- the task must stay untouched at REVIEW, and rescued must report
        False (no rescue actually happened), not True.
        """
        ticket_id, session_id = "GH-1019-mismatch-parked", "sess-1019-mismatch-parked"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.REVIEW,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.stage == Stage.REVIEW
        assert t.disposition is None

    def test_running_task_stage_mismatch_returns_not_routed(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + mismatched sentinel → SentinelRouteOutcome(False, False) (#1019).

        The task stays RUNNING (no status transition, no disposition stamp) --
        a true no-op on the mismatch path, mirroring the parked-task refusal.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1019-mismatch-running", "sess-1019-mismatch-running"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.REVIEW,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.RUNNING
        assert t.stage == Stage.REVIEW
        assert t.disposition is None

    def test_apply_sentinel_to_task_target_none_returns_routed_true(
        self, tmp_config_dir: Path
    ) -> None:
        """No matching task found → SentinelRouteOutcome(rescued=False, routed=True).

        Regression guard: a lookup miss has nothing to refuse, so `routed` must
        stay True -- callers that gate session completion on `routed` (the
        phantom sweep, #1019) must still complete the session in this case,
        matching pre-#1019 behavior for an unmatched ticket_id/session_id pair.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(
            "GH-1019-no-target", "sess-no-target", sentinel
        )

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)


class TestApplySentinelToTaskRoutedFalseFailedRace:
    """GitHub #1189: `routed` must reflect a FAILED-landing BlockedResult write,
    and a lookup miss must distinguish "raced to terminal" from "no such task".

    Pre-#1189, ``_route_blocked_result_to_task`` returned ``None`` and its sole
    call site discarded the value, so ``routed`` stayed at its default ``True``
    even when the call just landed the task terminal-FAILED. Separately, the
    lookup-miss branch conflated "no matching task at all" with "a matching
    task exists but is outside OCCUPIED_LANE_STATUSES" (raced to terminal by a
    concurrent caller) -- both returned ``routed=True``. Both must now report
    ``routed=False`` so callers do not complete/rescue a session whose task
    independently landed FAILED.
    """

    def test_running_task_blocked_result_deterministic_failure_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + deterministic parse-failure BlockedResult → routed=False."""
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-schema", "sess-1189-schema"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="schema_version_unsupported",
                details="test: unsupported schema_version",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        assert outcome.rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the last_blocked_result diagnostic write is scoped to
        # the unrecognized-reason catch-all only -- this deterministic-parse-
        # failure branch is untouched and must leave it unset.
        assert t.last_blocked_result is None

    def test_running_task_blocked_result_validation_failed_at_cap_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + validation_failed at the attempt cap → routed=False, FAILED."""
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-vfcap", "sess-1189-vfcap"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=_VALIDATION_FAILED_MAX_ATTEMPTS,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_VALIDATION_FAILED,
                details="test: validation failed at cap",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the last_blocked_result diagnostic write is scoped to
        # the unrecognized-reason catch-all only -- this validation_failed-
        # at-cap branch is untouched and must leave it unset.
        assert t.last_blocked_result is None

    def test_running_task_blocked_result_unknown_reason_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + unrecognised blocker reason → routed=False, FAILED.

        Companion/superset of ``test_signal_stop_unknown_blocker_reason_marks_
        failed`` (tests/test_cli.py), which pins the same call through the real
        CLI-adjacent path and now also asserts ``outcome.routed is False``.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-unknown", "sess-1189-unknown"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="unknown_reason_xyz",
                details="test: unrecognised reason code",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the unrecognized-reason catch-all now persists the
        # rejected sentinel.
        assert t.last_blocked_result == sentinel.model_dump(mode="json")

    def test_route_blocked_result_catch_all_writes_last_blocked_result(
        self, tmp_config_dir: Path
    ) -> None:
        """GitHub #1266: the unrecognized-reason catch-all persists the
        rejected sentinel.

        Calls _route_blocked_result_to_task directly with an
        unrecognized-reason BlockedResult. Confirms the existing FAILED/
        abandoned landing is unchanged AND the new diagnostic write lands
        so an operator can distinguish "no sentinel yet"
        (last_blocked_result=None) from "a rejected sentinel landed this
        FAILED."
        """
        from cw.reconcile._shared import _route_blocked_result_to_task

        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        target = TicketTask(
            ticket_id="GH-1266-catchall",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-1266-catchall",
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="status_unknown",
                details="test: unresolved placeholder sentinel",
            )
        )

        routed = _route_blocked_result_to_task(target, sentinel)

        assert routed is False
        assert target.status == QueueItemStatus.FAILED
        assert target.disposition == "abandoned"
        assert target.last_blocked_result == sentinel.model_dump(mode="json")

    def test_apply_sentinel_to_task_race_already_failed_task_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """A same-ticket/session task already raced to FAILED → routed=False.

        Simulates a concurrent winner's ``_route_blocked_result_to_task`` write
        landing FAILED just before this caller's own lookup runs. The task row
        must be left byte-for-byte unchanged -- this call has nothing to route.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-race", "sess-1189-race"
        completed_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
            completed_at=completed_at,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        # An arbitrary valid sentinel -- must never reach the dispatch branch,
        # since the lookup misses (task is outside OCCUPIED_LANE_STATUSES).
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome == SentinelRouteOutcome(rescued=False, routed=False)
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        assert t.completed_at == completed_at

    def test_apply_sentinel_to_task_race_miss_logs_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A race-miss (matched-but-excluded lookup) logs a WARNING.

        Companion to ``test_apply_sentinel_to_task_race_already_failed_task_
        returns_routed_false``: this pins the operator-visibility signal
        added alongside that fix -- before it, this branch resolved with no
        signal at all distinguishing it from "no such task ever existed."
        """
        import logging

        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-race-log", "sess-1189-race-log"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        with caplog.at_level(logging.WARNING):
            outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        assert any(
            "sentinel_race_miss_detected" in rec.message for rec in caplog.records
        )
        assert any(ticket_id in rec.message for rec in caplog.records)

    def test_apply_sentinel_to_task_unrelated_task_present_still_returns_routed_true(
        self, tmp_config_dir: Path
    ) -> None:
        """A non-empty store with no matching row still reports routed=True.

        Companion to ``test_apply_sentinel_to_task_target_none_returns_routed_
        true`` (which pins the fully-empty-store case): this pins the
        non-empty-store, no-match variant, proving the loop doesn't
        false-positive the race flag on an unrelated row.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        other_task = TicketTask(
            ticket_id="GH-1189-other",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-1189-other",
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[other_task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(
            "GH-1189-no-match", "sess-1189-no-match", sentinel
        )

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)

    def test_lookup_excluded_row_before_occupied_row_still_routes(
        self, tmp_config_dir: Path
    ) -> None:
        """An excluded-status row preceding the occupied match must not short-
        circuit the loop to a false race-miss (post-review amendment A2).

        Seeds TWO tasks sharing the same ticket_id+session_id shape -- an
        excluded-status (FAILED) row ordered before the occupied (RUNNING)
        row -- and asserts the loop keeps scanning and routes to the occupied
        match rather than reporting routed=False on the first (excluded) hit.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-order", "sess-1189-order"
        excluded_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
        )
        occupied_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[excluded_row, occupied_row]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is True
        t = next(
            t
            for t in load_dev_queue().tasks
            if t.ticket_id == ticket_id and t.status != QueueItemStatus.FAILED
        )
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_lookup_terminal_row_with_cleared_session_id_falls_to_true_miss(
        self, tmp_config_dir: Path
    ) -> None:
        """A terminal row whose session_id was cleared must not match the
        excluded-status branch (post-review amendment A3, soundness pin).

        The R3 lookup split's safety rests on an assumed precondition: every
        write that lands a task terminal with a cleared session_id
        (``cancel_ticket``/``cancel_task_for_session``/the PENDING branches of
        ``_route_blocked_result_to_task``) clears ``session_id`` in the SAME
        write as the status transition, so a terminal row should never carry a
        session_id that still matches an in-flight caller's lookup. This test
        exercises that assumed precondition directly (not a system-wide
        guarantee across every write site): given a CANCELLED task with
        session_id=None, the lookup must fall through to the truly-absent
        (routed=True) case, not the excluded-status-match (routed=False) case.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-cleared", "sess-1189-cleared"
        stale_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.CANCELLED,
            session_id=None,
            stage=Stage.IMPL,
            disposition="cancelled",
        )
        save_dev_queue(DevQueueStore(tasks=[stale_row]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)


class TestVerifySupervisorSessionId:
    """_verify_supervisor_session_id compares stored csid against supervisor state."""

    def _mk_daemon_session(
        self,
        sid: str,
        surface_ref: str | None,
        claude_session_id: str | None,
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> Session:
        return Session(
            id=sid,
            name=f"client-a/auto-dev/{sid}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=status,
            workspace_path=ClientConfig(
                name="client-a", workspace_path=Path("/tmp/ws")
            ).workspace_path,
            surface_ref=surface_ref,
            claude_session_id=claude_session_id,
            started_at=datetime(2026, 4, 19, tzinfo=UTC),
        )

    def _write_supervisor_state(
        self, jobs_path: Path, short_id: str, resume_id: str
    ) -> None:
        job_dir = jobs_path / short_id
        job_dir.mkdir(parents=True)
        (job_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": resume_id}),
            encoding="utf-8",
        )

    def test_matching_id_is_noop(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resumeSessionId matches claude_session_id — no mutation, no event."""
        short_id = "a1b2c3d4"
        full_uuid = "a1b2c3d4-0000-0000-0000-000000000001"
        jobs_path = tmp_config_dir / "jobs"
        self._write_supervisor_state(jobs_path, short_id, full_uuid)

        session = self._mk_daemon_session("s1", short_id, full_uuid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: full_uuid if sid == short_id else None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert load_state().sessions[0].surface_ref == short_id

    def test_mismatch_clears_claude_session_id(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mismatch: claude_session_id cleared, surface_ref left intact."""
        short_id = "b2c3d4e5"
        stored_csid = "b2c3d4e5-0000-0000-0000-000000000001"
        supervisor_resume_id = "ffffffff-dead-beef-dead-beefdeadbeef"

        session = self._mk_daemon_session("s2", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: supervisor_resume_id if sid == short_id else None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 1
        updated = load_state().sessions[0]
        assert updated.claude_session_id is None
        assert updated.surface_ref == short_id

    def test_mismatch_logs_warning(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """On mismatch, a warning containing 'csid_mismatch' is logged."""
        import logging

        short_id = "c3d4e5f6"
        stored_csid = "c3d4e5f6-0000-0000-0000-000000000001"
        supervisor_resume_id = "ffffffff-dead-beef-dead-beefdeadbeef"

        session = self._mk_daemon_session("s3", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: supervisor_resume_id if sid == short_id else None,
        )
        with caplog.at_level(logging.WARNING):
            _verify_supervisor_session_id(load_state())

        assert any("csid_mismatch" in rec.message for rec in caplog.records)

    def test_missing_state_json_is_noop(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No state.json → no continuity claim → no mutation."""
        short_id = "d4e5f6a7"
        stored_csid = "d4e5f6a7-0000-0000-0000-000000000001"

        session = self._mk_daemon_session("s4", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda _sid, **_kw: None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert load_state().sessions[0].surface_ref == short_id

    def test_no_claude_session_id_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session without claude_session_id is skipped — nothing to compare."""
        short_id = "e5f6a7b8"
        session = self._mk_daemon_session("s5", short_id, None)
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_no_surface_ref_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session without surface_ref has nothing to look up — skipped."""
        full_uuid = "f6a7b8c9-0000-0000-0000-000000000001"
        session = self._mk_daemon_session("s6", None, full_uuid)
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_completed_session_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-live (COMPLETED) sessions are not checked."""
        short_id = "a7b8c9d0"
        stored_csid = "a7b8c9d0-0000-0000-0000-000000000001"
        session = self._mk_daemon_session(
            "s7", short_id, stored_csid, status=SessionStatus.COMPLETED
        )
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_user_origin_session_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-DAEMON (USER) sessions are not checked."""
        short_id = "b8c9d0e1"
        stored_csid = "b8c9d0e1-0000-0000-0000-000000000001"
        session = Session(
            id="s8",
            name="client-a/auto-dev/s8",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.USER,
            status=SessionStatus.ACTIVE,
            workspace_path=ClientConfig(
                name="client-a", workspace_path=Path("/tmp/ws")
            ).workspace_path,
            surface_ref=short_id,
            claude_session_id=stored_csid,
            started_at=datetime(2026, 4, 19, tzinfo=UTC),
        )
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []


# ---------------------------------------------------------------------------
# _parse_sentinel_from_blocks — last-match + documented-example skip (#591)
# ---------------------------------------------------------------------------


def _documented_example_salvage_payload() -> dict[str, Any]:
    """Payload matching the illustrative example in the /auto-dev skill prompt."""
    return {
        "schema_version": 4,
        "ticket_id": "PROJ-1234",
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 42,
            "lines_actual": 47,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/proj-1234-fix-login",
        "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
        "fork_point_sha": "abc1234",
        "commits": ["sha1", "sha2"],
        "pr": {
            "number": 42,
            "url": "https://github.com/.../pull/42",
            "auto_merge": True,
            "base": "main",
        },
        "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "MEDIUM",
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


def _make_sentinel_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an assistant record wrapping *payload* as a sentinel text block."""
    body = json.dumps(payload)
    frame = f"<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"narrative\n{frame}"}],
        },
    }


def test_parse_sentinel_from_blocks_last_match_skips_documented_example(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-block: documented example first, real sentinel second → real one returned.

    Regression for GitHub #591: the live-guard monitor latched onto the
    illustrative pr=42/PROJ-1234 example block instead of the real result.
    last-match + is_documented_example skip must return the real sentinel.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-591"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    example_record = _make_sentinel_record(_documented_example_salvage_payload())
    path = _write_salvage_transcript(
        home,
        worktree,
        "claude-591",
        _shipped_salvage_payload(),
        extra_records=[example_record],
    )
    sess = _mk_headless_daemon_session("591", worktree, started_at)
    save_state(CwState(sessions=[sess]))

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "salv-1"
    assert result.pr is not None
    assert result.pr.number == 99


def test_parse_sentinel_from_blocks_example_only_returns_none(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the documented example block present → treated as no sentinel (None).

    Regression for GitHub #591: a freshly-spawned session with only the
    prompt's illustrative block should not report as shipped.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-591b"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    path = _write_salvage_transcript(
        home,
        worktree,
        "claude-591b",
        _documented_example_salvage_payload(),
    )
    sess = _mk_headless_daemon_session("591b", worktree, started_at)
    save_state(CwState(sessions=[sess]))

    result = _parse_sentinel_from_blocks(path)
    assert result is None


# ---------------------------------------------------------------------------
# _parse_sentinel_from_blocks — placeholder doc-example skip (GitHub #1266)
# ---------------------------------------------------------------------------

# Verbatim auto-dev-impl.md "Stage 2 Completion" worked example: ticket_id
# and status are angle-bracket placeholders. Fails schema validation (status
# not a known enum value) and, absent the fix, would parse to
# BlockedResult(status_unknown) -- landing a RUNNING task terminal-FAILED.
_PLACEHOLDER_EXAMPLE_BLOCK = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 4,\n'
    '  "ticket_id": "<ticket-id>",\n'
    '  "status": "<stage_complete | blocked>",\n'
    '  "stage_reached": "stage2_impl",\n'
    '  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0,'
    ' "lines_actual": 0, "forbidden_touched": false},\n'
    '  "plan_source": "<github_issue_existing | generated | free_text | none>",\n'
    '  "branch": "<branch-name>",\n'
    '  "worktree_path": "<session worktree path>",\n'
    '  "fork_point_sha": "<fork point sha>",\n'
    '  "commits": ["<sha1>", "<sha2>"],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {\n'
    '    "lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",\n'
    '    "any_incomplete_risk": false,\n'
    '    "shortcuts": [],\n'
    '    "recommendation": "PROCEED",\n'
    '    "downgrade_applied": false,\n'
    '    "fix_loop_escalated": false\n'
    "  },\n"
    '  "friction_highlights": [],\n'
    '  "ambiguities": [],\n'
    '  "blocker": null,\n'
    '  "prior_pr_warnings": [],\n'
    '  "next_actions": []\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)


def _write_raw_sentinel_transcript(path: Path, texts: list[str]) -> None:
    """Write a transcript at *path*, one assistant-text record per entry.

    Takes pre-built raw sentinel-block text directly rather than a payload
    dict -- GitHub #1266's placeholder tests need exact fidelity to the
    verbatim doc-example text, which is not valid against any AutoDevResult
    payload shape (its values are unresolved template placeholders).
    """
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"narrative\n{text}"}],
            },
        }
        for text in texts
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_parse_sentinel_from_blocks_placeholder_example_only_returns_none(
    tmp_path: Path,
) -> None:
    """GitHub #1266: only the doc-example placeholder block present -> None.

    Absent the raw-text pre-parse skip, this would parse to
    BlockedResult(status_unknown) since is_documented_example() cannot
    inspect a BlockedResult's fields (it has none matching the check).
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK])

    assert _parse_sentinel_from_blocks(path) is None


def test_parse_sentinel_from_blocks_placeholder_then_real_returns_real(
    tmp_path: Path,
) -> None:
    """GitHub #1266: placeholder block then a real sentinel -> the real result.

    Confirms last-match semantics survive the new placeholder skip.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    real_payload = _stage_complete_payload()
    real_frame = f"<<<AUTO_DEV_RESULT\n{json.dumps(real_payload)}\nAUTO_DEV_RESULT>>>"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK, real_frame])

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.status == "stage_complete"


def test_placeholder_sentinel_does_not_fail_running_task(tmp_path: Path) -> None:
    """GitHub #1266 acceptance test: a placeholder-only transcript must never
    reach _apply_sentinel_to_task and land a RUNNING task FAILED.

    Two-part proof (a literal "call _parse_sentinel_from_blocks, then call
    _apply_sentinel_to_task" chain is not a valid third step here: the scan
    correctly returns None for a placeholder-only transcript, and
    _apply_sentinel_to_task's own signature -- sentinel: AutoDevResult |
    BlockedResult, no Optional -- rejects None. A live call with a None
    result would be a mypy --strict violation, not a demonstration; the
    None return, combined with that signature, IS the safety proof):
    1. Directly parsing the verbatim placeholder text (bypassing the scan
       loop's skip) confirms it WOULD produce BlockedResult(status_unknown)
       -- documents the bug mechanism this fix closes.
    2. The scan loop (_parse_sentinel_from_blocks) returns None for a
       placeholder-only transcript. Every real ROUTE_EMITTED_SENTINEL caller
       (idle.py, stalled.py, phantom.py) guards on `if parsed is not None`
       before invoking _apply_sentinel_to_task, so a None return makes the
       call structurally skipped in production -- and, per the signature
       above, structurally impossible to make otherwise. A RUNNING task can
       therefore never be routed to FAILED off this block.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    # Part 1: prove the bug mechanism (unresolved placeholder -> status_unknown).
    direct = parse_stdout(_PLACEHOLDER_EXAMPLE_BLOCK)
    assert isinstance(direct, BlockedResult)
    assert direct.blocker.reason == "status_unknown"

    # Part 2: the scan loop must never surface it as a routable result.
    path = tmp_path / "transcript.jsonl"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK])
    assert _parse_sentinel_from_blocks(path) is None


def test_1258_shape_placeholder_then_delayed_real_sentinel(tmp_path: Path) -> None:
    """GitHub #1266 / #1258 timeline: placeholder at T, real sentinel at T+1.

    A worker quotes the doc example while narrating, then emits the real
    stage_complete sentinel later in the same transcript -- the scan must
    return the real result, not None and not the placeholder's BlockedResult.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    real_payload = _stage_complete_payload()
    real_frame = f"<<<AUTO_DEV_RESULT\n{json.dumps(real_payload)}\nAUTO_DEV_RESULT>>>"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK, real_frame])

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.status == "stage_complete"
    assert result.ticket_id == real_payload["ticket_id"]


class TestFinalizeBlocked:
    """Tests for finalize-blocked detection + rescue (GitHub #812)."""

    # ── shared setup helpers ──────────────────────────────────────────────

    def _mk_finalize_session(
        self, sid: str, ticket_id: str, worktree: Path, started_at: datetime
    ) -> Session:
        """Return an ACTIVE DAEMON FINALIZE-stage session past budget.

        Name uses ticket_id (not sid) so ticket_id_for_session() resolves correctly.
        """
        sess = Session(
            id=sid,
            name=f"client-a/auto-dev/{ticket_id}",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="surf-ref",
            started_at=started_at,
        )
        context_dir = worktree / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            '{"headless": true, "session_id": "' + sid + '"}'
        )
        return sess

    def _mk_finalize_task(
        self, ticket_id: str, sid: str, *, lane: str = DEFAULT_LANE
    ) -> TicketTask:
        return TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=sid,
            stage=Stage.FINALIZE,
            lane=lane,
        )

    # ── 1.1 happy path ───────────────────────────────────────────────────

    def test_finalize_blocked_happy_path(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FINALIZE + commits + no PR → TIMED_OUT, BLOCKED_ON_USER, FINALIZE_BLOCKED."""
        worktree = tmp_path / "wt-fb-1"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-1"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-1", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = self._mk_finalize_task(ticket_id, "fb-sess-1", lane="finalize-lane")
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        push_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification",
            lambda name, _client: push_calls.append(name),
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id not in reverted  # not reverted to PENDING

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == "fb-sess-1")
        assert s.status == SessionStatus.TIMED_OUT
        assert s.reap_reason == ReapReason.FINALIZE_BLOCKED
        assert isinstance(s.last_result, dict)
        assert s.last_result.get("paused_status") == "finalize_blocked"
        assert "dev/FB-1" in str(s.last_result.get("branch", ""))

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.session_id is None

        assert push_calls  # push notification fired

        events = read_events(
            consumer="test-fb-1-attn",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "finalize-lane"

    # ── 1.2 no commits → REVERT_TASK ─────────────────────────────────────

    def test_no_commits_falls_through_to_revert(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No commits beyond base → REVERT_TASK (not FINALIZE_BLOCKED)."""
        worktree = tmp_path / "wt-fb-2"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-2"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-2", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-2")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: False
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted  # reverted to PENDING (REVERT_TASK path)
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.3 PR exists → REVERT_TASK ──────────────────────────────────────

    def test_pr_exists_falls_through_to_revert(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR already open → REVERT_TASK (not FINALIZE_BLOCKED)."""
        worktree = tmp_path / "wt-fb-3"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-3"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-3", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-3")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        # PR exists → (True, True): not finalize-blocked
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (True, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.4 Stage.IMPL → REVERT_TASK ─────────────────────────────────────

    def test_impl_stage_not_finalize_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stage.IMPL → REVERT_TASK regardless of commits/PR state."""
        worktree = tmp_path / "wt-fb-4"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-4"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-4", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        impl_task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-4",
            stage=Stage.IMPL,  # IMPL, not FINALIZE
        )
        save_dev_queue(DevQueueStore(tasks=[impl_task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (False, True),
        )

        reverted = revert_stalled_headless_sessions(
            load_state(), now=now, config=_auto_config()
        )

        assert ticket_id in reverted
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING

    # ── 1.5 gh unavailable → _GH_CHECK_BLOCKED_REASON ───────────────────

    def test_gh_unavailable_routes_to_gh_check_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh absent → fall-through to REVERT_TASK → gh_check_blocked."""
        worktree = tmp_path / "wt-fb-5"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-5"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-5", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-5")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        # gh unavailable in _resolve_finalize_blocked_condition
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )
        # gh also unavailable in revert_stalled_headless_sessions pre-pass
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_a, **_kw: (None, False),
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        # gh_blocked_revert_candidates path → BLOCKED_ON_USER
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

        events = read_events(
            consumer="test-fb-5-needs-attention",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn = [e for e in events if e.payload.get("ticket_id") == ticket_id]
        assert any(e.payload.get("paused_status") == "gh_check_blocked" for e in attn)

    # ── 1.6 worktree NOT cleaned up ──────────────────────────────────────

    def test_worktree_preserved(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finalize-blocked: _cleanup_timed_out_worktree is NOT called."""
        worktree = tmp_path / "wt-fb-6"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-6"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-6", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-6")])
        )

        cleanup_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._cleanup_timed_out_worktree",
            lambda s, _tid: cleanup_calls.append(s.id),
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        assert "fb-sess-6" not in cleanup_calls

    # ── 1.7 breadcrumbs contains branch ──────────────────────────────────

    def test_needs_attention_breadcrumbs_contains_branch(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SESSION_NEEDS_ATTENTION breadcrumbs contain the feature branch name."""
        worktree = tmp_path / "wt-fb-7"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-7"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-7", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-7")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        events = read_events(
            consumer="test-fb-7-breadcrumbs",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        attn = [
            e
            for e in events
            if e.payload.get("paused_status") == "finalize_blocked"
            and e.payload.get("ticket_id") == ticket_id
        ]
        assert len(attn) == 1
        assert "dev/FB-7" in str(attn[0].payload.get("breadcrumbs", ""))

    # ── 1.8 idempotency ──────────────────────────────────────────────────

    def test_idempotency_second_tick_skips(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second tick: session is TIMED_OUT → skipped by _LIVE_STATUSES guard."""
        worktree = tmp_path / "wt-fb-8"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-8"

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-8", ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(tasks=[self._mk_finalize_task(ticket_id, "fb-sess-8")])
        )

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )

        # Tick 1: detect + act → TIMED_OUT
        revert_stalled_headless_sessions(load_state(), now=now, config=_auto_config())

        # Tick 2: session is TIMED_OUT; detect returns no candidates for it
        from cw.reconcile.stalled import _detect_stalled_candidates

        state2 = load_state()
        candidates2 = _detect_stalled_candidates(
            state2,
            now=now,
            config=_auto_config(),
            task_by_ticket={t.ticket_id: t for t in load_dev_queue().tasks},
        )
        fb_candidates = [c for c in candidates2 if c.session_id == "fb-sess-8"]
        assert not fb_candidates  # TIMED_OUT session skipped by _LIVE_STATUSES

    # ── 1.9 unit test for _resolve_finalize_blocked_condition ─────────────

    def test_resolve_condition_true_for_finalize_false_for_impl(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_resolve_finalize_blocked_condition: True for FINALIZE, False for IMPL."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-9"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-9", "FB-9", worktree, started_at)
        finalize_task = self._mk_finalize_task("FB-9", "fb-sess-9")
        impl_task = TicketTask(
            ticket_id="FB-9",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-9",
            stage=Stage.IMPL,
        )

        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", _capture_pr_exists
        )

        blocked, branch = _resolve_finalize_blocked_condition(
            finalize_task, sess, worktree, "main"
        )
        assert blocked is True
        assert branch is not None
        assert "FB-9" in branch
        # Fallback path gh call scoped to client-a's repo cwd (#1279).
        assert pr_cwds == [Path("/tmp/ws-staged")]

        blocked2, branch2 = _resolve_finalize_blocked_condition(
            impl_task, sess, worktree, "main"
        )
        assert blocked2 is False
        assert branch2 is None

    def test_resolve_condition_dangling_client_skips_gh_returns_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback path with a dangling client (clients.yaml populated but
        missing session.client) → pr_exists_for_branch NOT called, returns
        (False, None) → REVERT_TASK fallthrough (GitHub #1279 R7)."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-dangling"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        # clients.yaml populated with a DIFFERENT client than the session's.
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        sess = self._mk_finalize_session("fb-dangling", "FB-D", worktree, started_at)
        finalize_task = self._mk_finalize_task("FB-D", "fb-dangling")

        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        called: list[str] = []

        def _should_not_run(_b: str, **_kw: object) -> tuple[bool | None, bool]:
            called.append(_b)
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch", _should_not_run
        )

        blocked, branch = _resolve_finalize_blocked_condition(
            finalize_task, sess, worktree, "main"
        )
        assert blocked is False
        assert branch is None
        assert called == []

    # ── 1.10 rescue happy path ────────────────────────────────────────────

    def test_rescue_happy_path(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rescue_finalize_blocked_sessions: gh pr create + merge, task COMPLETED."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-10"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-10"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # TIMED_OUT session with finalize-blocked marker
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        pr_cwds: list[object] = []

        def _capture_pr_exists(_b: str, **kw: object) -> tuple[bool | None, bool]:
            pr_cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", _capture_pr_exists
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed
        # gh call scoped to client-a's repo, not the ambient CWD (#1279).
        assert pr_cwds == [Path("/tmp/ws-staged")]

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED
        assert s.reap_reason == ReapReason.FINALIZE_BLOCKED

        # Verify gh pr create and gh pr merge were called
        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert create_calls
        assert merge_calls

        events = read_events(
            consumer="test-fb-10-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        rescue_events = [
            e
            for e in events
            if e.payload.get("salvage_kind") == "finalize_blocked_rescue"
        ]
        assert len(rescue_events) == 1

    # ── 1.10b merged ticket parked finalize-blocked → complete, no PR ──────

    def test_rescue_finalize_blocked_merged_during_park_no_duplicate_pr(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids short-circuits before the OPEN-only pr_exists_for_branch
        check (#1054) -- completes the session/task directly without opening or
        merging a PR (skip_merge=True), avoiding a duplicate _rescue_open_pr call."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-MERGED-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-merged-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id in completed
        pr_check.assert_not_called()

        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert create_calls == []
        assert merge_calls == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED

        events = read_events(
            consumer=f"test-rescue-merged-{sid}",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("skip_merge") is True

    def test_rescue_finalize_blocked_merged_does_not_complete_other_clients_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete's dev-queue task lookup is (client, ticket_id)
        scoped (#1054): when a DIFFERENT client also has a BLOCKED_ON_USER
        task sharing this ticket_id string, only the merged session's own
        client's task is completed -- the other client's task must be left
        untouched, not silently marked COMPLETED by the bare ticket_id match
        that pre-existed in _rescue_complete."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-COLLIDE-COMPLETE-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-collide-complete-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task_a = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        # client-b's own BLOCKED_ON_USER task happens to share this ticket_id
        # string -- must survive untouched. Listed BEFORE task_a: _rescue_complete
        # `break`s on its first dev-queue match, so without client-scoping this
        # ordering would complete task_b (wrong client) and never reach task_a.
        task_b = TicketTask(
            ticket_id=ticket_id,
            client="client-b",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task_b, task_a]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            MagicMock(return_value=MagicMock(returncode=0, stdout="")),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch",
            MagicMock(return_value=(False, True)),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)})
        )

        assert ticket_id in completed

        store = load_dev_queue()
        reloaded_a = next(
            t
            for t in store.tasks
            if t.client == "client-a" and t.ticket_id == ticket_id
        )
        reloaded_b = next(
            t
            for t in store.tasks
            if t.client == "client-b" and t.ticket_id == ticket_id
        )
        assert reloaded_a.status == QueueItemStatus.COMPLETED
        # The regression this test guards against: pre-fix, _rescue_complete's
        # bare `task.ticket_id == ticket_id` match (no client filter) would
        # complete whichever BLOCKED_ON_USER task it iterated to first.
        assert reloaded_b.status == QueueItemStatus.BLOCKED_ON_USER

    def test_rescue_finalize_blocked_different_client_ticket_not_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_client_ticket_ids for a DIFFERENT client must not complete
        this session's same-numbered ticket (cross-client collision guard,
        #1054) -- the normal PR-open flow must run instead."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-COLLIDE-1"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-collide-1"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        pr_check = MagicMock(return_value=(False, True))
        monkeypatch.setattr("cw.reconcile.salvage.pr_exists_for_branch", pr_check)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # No existing PR + not merged-for-this-client -> falls through to
        # _rescue_open_pr's real gh subprocess call; force it to fail so the
        # session is tombstoned as rescue_attempted rather than completed.
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, ["gh"])),
        )

        # client-b's same-numbered ticket is merged; this session is client-a's.
        rescue_finalize_blocked_sessions(
            merged_client_ticket_ids=frozenset({("client-b", ticket_id)})
        )

        # Falls through to the normal PR-existence check rather than the
        # merged-ticket skip_merge=True completion shortcut.
        pr_check.assert_called_once()

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.TIMED_OUT

    # ── 1.11 PR already exists → skip create, still merge ─────────────────

    def test_rescue_pr_already_exists(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR already open → skip gh pr create, still call gh pr merge."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-11"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-11"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        gh_args_seen: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **_kw: object) -> MagicMock:
            gh_args_seen.append(list(args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_subprocess_run)
        # PR already exists → (True, True)
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (True, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed

        create_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "create"]]
        merge_calls = [a for a in gh_args_seen if a[:3] == ["gh", "pr", "merge"]]
        assert not create_calls  # create skipped
        assert merge_calls  # merge still called

    # ── 1.12 gh pr create fails → rescue_attempted, task stays BLOCKED ───

    def test_rescue_create_fails_marks_attempted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr create fails → rescue_attempted=True, task stays BLOCKED_ON_USER."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        ticket_id = "FB-12"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-12"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        import subprocess as _subprocess

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda _args, **_kw: (_ for _ in ()).throw(
                _subprocess.CalledProcessError(1, "gh")
            ),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id not in completed

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER  # unchanged

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert isinstance(s.last_result, dict)
        assert s.last_result.get("rescue_attempted") is True

    # ── 1.13 _rescue_mark_attempted: non-dict last_result ────────────────

    def test_rescue_mark_attempted_non_dict_last_result(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_mark_attempted: when last_result is not a dict, set to dict."""
        from cw.reconcile.salvage import _rescue_mark_attempted

        sid = "fb-sess-13"
        ticket_id = "FB-13"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        # last_result=None (default) → else branch sets it to {"rescue_attempted": True}
        save_state(CwState(sessions=[sess]))

        _rescue_mark_attempted(sid)

        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.last_result == {"rescue_attempted": True}

    # ── 1.14 _rescue_complete: not-mutated early return (race) ───────────

    def test_rescue_complete_not_mutated_returns_early(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: session already COMPLETED under lock → no event emitted."""
        from cw.reconcile.salvage import _rescue_complete

        sid = "fb-sess-14"
        ticket_id = "FB-14"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # State has session already COMPLETED (simulates concurrent completion).
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.status = SessionStatus.COMPLETED
        save_state(CwState(sessions=[sess]))

        completed_ticket_ids: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        # _rescue_complete calls gh pr merge BEFORE the lock guard; patch to
        # avoid a real subprocess invocation in this race-condition test path.
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda *_a, **_kw: MagicMock(returncode=0, stdout=""),
        )

        stale_sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        _rescue_complete(stale_sess, ticket_id, branch, completed_ticket_ids)

        assert not completed_ticket_ids
        events = read_events(
            consumer="test-fb-14-no-event",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == sid for e in events)

    # ── 1.15 _rescue_complete: surface_ref set → daemon stop ─────────────

    def test_rescue_complete_daemon_stop_on_surface_ref(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: surface_ref present → daemon.stop() is called."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-15"
        ticket_id = "FB-15"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        sess.surface_ref = "surf-ref-15"
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda _a, **_kw: MagicMock(returncode=0, stdout=""),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )

        rescue_finalize_blocked_sessions()

        assert daemon.stop_calls

    # ── 1.16 merge failure swallowed, session still completed ─────────────

    def test_rescue_merge_failure_swallowed(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh pr merge CalledProcessError is swallowed; session still COMPLETED."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-16"
        ticket_id = "FB-16"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.BLOCKED_ON_USER,
                    )
                ]
            )
        )

        import subprocess as _sub

        def _fake_run(args: list[str], **_kw: object) -> MagicMock:
            if args[:3] == ["gh", "pr", "merge"]:
                raise _sub.CalledProcessError(1, "gh")
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed = rescue_finalize_blocked_sessions()

        assert ticket_id in completed
        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert s.status == SessionStatus.COMPLETED

    # ── 1.17 rescue filter edge cases ────────────────────────────────────

    def test_rescue_filter_wrong_paused_status(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: wrong paused_status → session skipped."""
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17a"
        ticket_id = "FB-17A"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": "some_other_reason",
            "branch": f"dev/{ticket_id}",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_rescue_attempted(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: rescue_attempted=True → session skipped."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17b"
        ticket_id = "FB-17B"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": f"dev/{ticket_id}",
            "rescue_attempted": True,
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_empty_branch(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: empty branch → session skipped."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17c"
        ticket_id = "FB-17C"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": "",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_unknown_client(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """rescue_finalize_blocked_sessions: unknown client → log warning, skip."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17d"
        ticket_id = "FB-17D"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.client = "no-such-client"
        sess.name = f"no-such-client/auto-dev/{ticket_id}"
        sess.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": f"dev/{ticket_id}",
        }
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        completed = rescue_finalize_blocked_sessions()
        assert ticket_id not in completed

    def test_rescue_filter_gh_unavailable(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh unavailable → session skipped, rescue_attempted not written."""
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON
        from cw.reconcile.salvage import rescue_finalize_blocked_sessions

        sid = "fb-sess-17e"
        ticket_id = "FB-17E"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        completed = rescue_finalize_blocked_sessions()

        assert completed == []
        # gh-unavailable is transient — rescue_attempted must NOT be tombstoned.
        reloaded = load_state()
        s = next(s for s in reloaded.sessions if s.id == sid)
        assert not (
            isinstance(s.last_result, dict) and s.last_result.get("rescue_attempted")
        )

    # ── 1.18 _resolve_finalize_blocked_condition with pre-computed dict ───

    def test_resolve_uses_finalize_pr_by_branch_dict(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_resolve_finalize_blocked_condition uses pre-computed dict when provided."""
        from cw.reconcile.stalled import _resolve_finalize_blocked_condition

        worktree = tmp_path / "wt-fb-18"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = self._mk_finalize_session("fb-sess-18", "FB-18", worktree, started_at)
        task = self._mk_finalize_task("FB-18", "fb-sess-18")

        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )

        branch = "dev/FB-18"
        finalize_pr_by_branch = {branch: (False, True)}

        blocked, result_branch = _resolve_finalize_blocked_condition(
            task,
            sess,
            worktree,
            "main",
            finalize_pr_by_branch=finalize_pr_by_branch,
        )
        assert blocked is True
        assert result_branch == branch

    # ── 1.19 _apply_finalize_blocked_queue_mutations: filter branches ─────

    def test_apply_finalize_blocked_queue_mutations_filter_branches(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_apply_finalize_blocked_queue_mutations: filter branches."""
        from cw.models import ReapReason
        from cw.reconcile._shared import ProposedAction, ReapCandidate
        from cw.reconcile.stalled import _apply_finalize_blocked_queue_mutations

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        candidate = ReapCandidate(
            session_id="fb-sess-19",
            proposed_action=ProposedAction.PARK_FINALIZE_BLOCKED,
            ticket_id="FB-19",
            elapsed_seconds=7200,
            reap_reason=ReapReason.FINALIZE_BLOCKED,
            lane="default",
            client="client-a",
        )

        task_target = TicketTask(
            ticket_id="FB-19",
            client="client-a",
            status=QueueItemStatus.RUNNING,
        )
        task_other = TicketTask(
            ticket_id="OTHER-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
        )
        task_not_running = TicketTask(
            ticket_id="FB-19",
            client="client-a",
            status=QueueItemStatus.PENDING,
        )
        save_dev_queue(DevQueueStore(tasks=[task_other, task_not_running, task_target]))

        _apply_finalize_blocked_queue_mutations([candidate])

        store = load_dev_queue()
        target_tasks = [t for t in store.tasks if t.ticket_id == "FB-19"]
        other_tasks = [t for t in store.tasks if t.ticket_id == "OTHER-1"]

        assert other_tasks[0].status == QueueItemStatus.RUNNING
        running_target = next(
            (t for t in target_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER),
            None,
        )
        assert running_target is not None
        # Non-RUNNING FB-19 task must stay PENDING (filter branch coverage).
        pending_target = next(
            (t for t in target_tasks if t.status == QueueItemStatus.PENDING),
            None,
        )
        assert pending_target is not None

    # ── 1.20 _build_finalize_pr_map direct tests ─────────────────────────

    def test_build_finalize_pr_map(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_build_finalize_pr_map: FINALIZE DAEMON session → calls pr_exists."""
        from cw.reconcile.core import _build_finalize_pr_map

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        worktree = tmp_path / "wt-fb-20"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        sess_finalize = self._mk_finalize_session(
            "fb-sess-20", "FB-20", worktree, started_at
        )
        sess_finalize.status = SessionStatus.ACTIVE

        # Session with no valid ticket_id in name → skipped (line 130)
        sess_no_tid = Session(
            id="fb-sess-20b",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            started_at=started_at,
        )

        task = TicketTask(
            ticket_id="FB-20",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []
        cwds: list[object] = []

        def _fake_pr_exists(branch: str, **kw: object) -> tuple[bool | None, bool]:
            calls.append(branch)
            cwds.append(kw.get("cwd"))
            return False, True

        monkeypatch.setattr("cw.reconcile.core.pr_exists_for_branch", _fake_pr_exists)

        state = CwState(sessions=[sess_finalize, sess_no_tid])

        result = _build_finalize_pr_map(state)

        assert calls, "pr_exists_for_branch should have been called"
        assert any("FB-20" in branch for branch in calls)
        assert any(pr is False for pr, _ in result.values())
        # sess_no_tid has no valid ticket_id → excluded; only 1 branch checked.
        assert len(calls) == 1
        # cwd scoped to client-a's _git_dir (workspace_path, no repo_path) (#1279).
        assert cwds == [Path("/tmp/ws-staged")]

    def test_build_finalize_pr_map_dangling_client_skips_gh_call(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_build_finalize_pr_map: session's client absent from a populated
        clients.yaml → branch never enters result, gh never called (#1279)."""
        from cw.reconcile.core import _build_finalize_pr_map

        # clients.yaml populated with a DIFFERENT client than the session's.
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        worktree = tmp_path / "wt-fb-dangling"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        sess_finalize = self._mk_finalize_session(
            "fb-dangling", "FB-DANGLING", worktree, started_at
        )
        sess_finalize.status = SessionStatus.ACTIVE

        task = TicketTask(
            ticket_id="FB-DANGLING",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _fake_pr_exists(branch: str, **_kw: object) -> tuple[bool | None, bool]:
            calls.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile.core.pr_exists_for_branch", _fake_pr_exists)

        result = _build_finalize_pr_map(CwState(sessions=[sess_finalize]))

        assert calls == []
        assert result == {}

    # ── 1.21 reconcile(): ticket_id=None and gh_blocked in pr_is_merged pass

    def test_reconcile_prepass_ticket_id_none_and_gh_blocked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile(): DAEMON sessions where ticket_id is None or gh is unavailable."""
        from cw.reconcile import reconcile

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Session A: DAEMON with name that yields no ticket_id → line 177 continue
        sess_no_tid = Session(
            id="fb-prepass-no-tid",
            name="client-a/impl",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-no-tid",
        )

        # Session B: gh returns unavailable → _gh_blocked_tids.
        # Session C below hits the gh_blocked branch (_gh_available now False).
        sess_gh_blocked = Session(
            id="fb-prepass-gh-blocked",
            name="client-a/auto-dev/FB-21-A",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-gh-blocked",
        )

        sess_second = Session(
            id="fb-prepass-second",
            name="client-a/auto-dev/FB-21-B",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            surface_ref="surf-second",
        )

        save_state(CwState(sessions=[sess_no_tid, sess_gh_blocked, sess_second]))
        save_dev_queue(DevQueueStore(tasks=[]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, False),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.pr_exists_for_branch", lambda _b, **_kw: (None, False)
        )

        report = reconcile()
        # No tasks → nothing completed; gh-unavailable pre-pass didn't corrupt state.
        assert report.completed_ticket_ids == []

    # ── 1.22 rescued_ticket_ids field on ReconcileReport (SHOULD_FIX 2) ────

    def test_rescue_ids_land_in_rescued_ticket_ids_not_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile(): rescued IDs go to rescued_ticket_ids, not completed."""
        from cw.reconcile import reconcile
        from cw.reconcile._shared import _FINALIZE_BLOCKED_REASON

        ticket_id = "FB-22"
        branch = f"dev/{ticket_id}"
        sid = "fb-sess-22"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.reap_reason = ReapReason.FINALIZE_BLOCKED
        sess.last_result = {"paused_status": _FINALIZE_BLOCKED_REASON, "branch": branch}
        save_state(CwState(sessions=[sess]))

        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile.salvage.subprocess.run",
            lambda *_a, **_kw: MagicMock(returncode=0, stdout=""),
        )
        monkeypatch.setattr(
            "cw.reconcile.salvage.pr_exists_for_branch", lambda _b, **_kw: (False, True)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.fire_push_notification", lambda *_a: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, True),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.pr_exists_for_branch", lambda _b, **_kw: (None, True)
        )
        monkeypatch.setattr("cw.reconcile._shared._claude_agents_json", list)

        report = reconcile()

        assert ticket_id in report.rescued_ticket_ids
        assert ticket_id not in report.completed_ticket_ids

    # ── 1.23 _rescue_complete: merge only after mutated guard (SHOULD_FIX 3) ──

    def test_rescue_complete_merge_skipped_on_concurrent_completion(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_rescue_complete: gh pr merge NOT called when session past TIMED_OUT."""
        from cw.reconcile.salvage import _rescue_complete

        sid = "fb-sess-23"
        ticket_id = "FB-23"
        branch = f"dev/{ticket_id}"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)

        _write_staged_clients_yaml(tmp_config_dir, "client-a")

        # Session is already COMPLETED — simulates concurrent completion race.
        sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        sess.status = SessionStatus.COMPLETED
        save_state(CwState(sessions=[sess]))

        merge_called: list[bool] = []

        def _fake_run(args: list[str], **_kw: object) -> MagicMock:
            if args[:3] == ["gh", "pr", "merge"]:
                merge_called.append(True)
            return MagicMock(returncode=0, stdout="")

        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        completed_ticket_ids: list[str] = []
        stale_sess = _mk_timed_out_daemon_session(sid, ticket_id, completed_at=now)
        _rescue_complete(stale_sess, ticket_id, branch, completed_ticket_ids)

        # mutated=False → returns early; gh pr merge must NOT have been called.
        assert not merge_called
        assert not completed_ticket_ids

    # ── 1.24 unknown client in finalize-blocked detect → skip (NIT 4) ───────

    def test_finalize_blocked_unknown_client_skip(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown client in _detect_stalled_candidates: skip, not 'main' fallback."""
        from cw.reconcile.stalled import _detect_stalled_candidates

        worktree = tmp_path / "wt-fb-24"
        worktree.mkdir(parents=True)
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)
        ticket_id = "FB-24"

        # No clients configured — all clients unknown.
        _write_staged_clients_yaml(tmp_config_dir, "other-client")

        # Session with a client that is NOT in the loaded effective_clients.
        sess = Session(
            id="fb-sess-24",
            name=f"unknown-client/auto-dev/{ticket_id}",
            client="unknown-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref="surf-24",
            started_at=started_at,
        )
        context_dir = worktree / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            '{"headless": true, "session_id": "fb-sess-24"}'
        )

        task = TicketTask(
            ticket_id=ticket_id,
            client="unknown-client",
            status=QueueItemStatus.RUNNING,
            session_id="fb-sess-24",
            stage=Stage.FINALIZE,
        )

        state = CwState(sessions=[sess])
        config = _auto_config()
        monkeypatch.setattr(
            "cw.reconcile.stalled._has_commits_beyond_base", lambda _p, _b: True
        )
        pr_calls: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile.stalled.pr_exists_for_branch",
            lambda b, **_kw: pr_calls.append(b) or (False, True),  # type: ignore[func-returns-value]
        )

        candidates = _detect_stalled_candidates(
            state,
            now=now,
            config=config,
            task_by_ticket={ticket_id: task},
        )

        # Unknown client → finalize-blocked detection skipped entirely.
        # Session should still be detected as REVERT_TASK (timed out).
        fb_candidates = [
            c for c in candidates if c.proposed_action.value == "park_finalize_blocked"
        ]
        assert not fb_candidates
        # pr_exists_for_branch must NOT have been called for this unknown client
        # (previously would have been called with the "main" fallback).
        assert not pr_calls

    # ── 1.25 _RESCUE_PR_BODY_TEMPLATE in _shared (NIT 5) ────────────────────

    def test_rescue_pr_body_template_in_shared(self) -> None:
        """_RESCUE_PR_BODY_TEMPLATE is importable from _shared (NIT 5)."""
        from cw.reconcile._shared import (
            _RESCUE_PR_BODY_TEMPLATE,
            _RESCUE_PR_CLOSES_TRAILER_TEMPLATE,
        )

        assert "finalize" in _RESCUE_PR_BODY_TEMPLATE.lower()
        assert "{ticket_id}" in _RESCUE_PR_BODY_TEMPLATE
        assert "Closes #" in _RESCUE_PR_CLOSES_TRAILER_TEMPLATE.format(ticket_id="1293")

    # ── 1.26 _rescue_open_pr Closes trailer wiring (#1293) ──────────────────

    def test_rescue_open_pr_numeric_ticket_includes_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real numeric ticket_id gets a 'Closes #<n>' trailer appended to
        the PR body so the auto-rescued PR auto-closes the linked issue on
        merge (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/1293", "main", "1293")

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #1293" in body

    def test_rescue_open_pr_unknown_ticket_id_omits_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None ticket_id must NOT get a Closes trailer -- falls back to the
        unconditional 'Ticket: #unknown'-style text via the untouched
        _RESCUE_PR_BODY_TEMPLATE (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/none-ticket", "main", None)

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #" not in body
        assert "unknown" in body

    def test_rescue_open_pr_nonnumeric_ticket_id_omits_closes_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric (e.g. Linear-style) ticket_id must NOT get a Closes
        trailer -- mirrors the real non-numeric ticket_id case proven live in
        test_rescue_happy_path, and is the exact case the ticket's root-cause
        analysis warns against (#1293)."""
        from cw.reconcile.salvage import _rescue_open_pr

        run_mock = MagicMock()
        monkeypatch.setattr("cw.reconcile.salvage.subprocess.run", run_mock)

        result = _rescue_open_pr("dev/fb-10", "main", "FB-10")

        assert result is True
        run_mock.assert_called_once()
        args = run_mock.call_args[0][0]
        body = args[args.index("--body") + 1]
        assert "Closes #" not in body
        assert "Ticket: #FB-10" in body


# ---------------------------------------------------------------------------
# _apply_idle_queue_mutations disposition stamping
# ---------------------------------------------------------------------------


def _mk_running_task(ticket_id: str, client: str = "client-a") -> TicketTask:
    task = TicketTask(
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


class TestParseAnySentinelFromTranscript:
    """Two-layer transcript search: csid-exact (Layer 1) then surface_ref (Layer 2).

    Regression for GitHub #892: a REVIEW worker that emits review_pending_approval
    and then spawns fanout subagents gets a new csid (V2) backfilled from the
    daemon roster.  The sentinel lives in the pre-resume V1 transcript; Layer 2
    must fall through to find it when V1 does not match the csid-exact path.
    """

    _SURFACE_REF = "surf1234"
    _STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def _write_sentinel_transcript(
        self, path: Path, status: str, ticket_id: str
    ) -> None:
        """Write a minimal sentinel-bearing JSONL to path."""
        payload = _make_terminal_payload(status, ticket_id)
        body = json.dumps(payload)
        frame = f"<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": frame}],
            },
        }
        path.write_text(json.dumps(record) + "\n")

    def _write_no_sentinel_transcript(self, path: Path) -> None:
        """Write a JSONL with no sentinel framing."""
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "narrative only, no sentinel"}],
            },
        }
        path.write_text(json.dumps(record) + "\n")

    def _mk_session(self, worktree: Path, csid: str | None = None) -> Session:
        return Session(
            id="892-sess",
            name="client-a/auto-dev/892",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref=self._SURFACE_REF,
            claude_session_id=csid,
            started_at=self._STARTED_AT,
        )

    def test_v2_csid_no_sentinel_v1_surface_ref_has_sentinel_returns_v1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Layer 2 fallthrough: csid transcript found but no sentinel; surface_ref
        transcript (distinct file) carries review_pending_approval → returned.

        Regression for #892: REVIEW worker emits sentinel in V1, then spawns
        subagents.  Backfill sets claude_session_id to V2 (resumed session).
        V2 transcript has no sentinel.  Layer 2 must find and return V1.
        """
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-layer2"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        # V2: csid transcript (distinct csid — does not match surface_ref glob)
        v2_csid = "v2-resumed-csid-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_no_sentinel_transcript(v2_path)

        # V1: surface_ref transcript (matches "surf1234*.jsonl" glob, has sentinel)
        v1_path = project_dir / f"{self._SURFACE_REF}-v1original.jsonl"
        self._write_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "review_pending_approval"
        assert csid_stem == f"{self._SURFACE_REF}-v1original"

    def test_neither_transcript_has_sentinel_returns_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both csid transcript and surface_ref transcript lack a sentinel → None."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-none"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-no-sentinel-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{self._SURFACE_REF}-v1-also-none.jsonl"
        self._write_no_sentinel_transcript(v1_path)

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is None

    def test_csid_transcript_with_sentinel_wins_over_surface_ref(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: when csid transcript has a sentinel, Layer 1 returns it
        immediately without consulting surface_ref (Layer 2 never fires)."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-layer1-wins"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        # csid transcript has sentinel (plan_pending_approval)
        v2_csid = "v2-has-sentinel-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_sentinel_transcript(v2_path, "plan_pending_approval", "892-plan")

        # surface_ref transcript also has a sentinel (review_pending) — should NOT win
        v1_path = project_dir / f"{self._SURFACE_REF}-v1-review.jsonl"
        self._write_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert csid_stem == v2_csid

    def test_csid_absent_surface_ref_only_returns_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No csid set — existing csid=None path still works via Layer 2 only."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-no-csid"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v1_path = project_dir / f"{self._SURFACE_REF}-original.jsonl"
        self._write_sentinel_transcript(v1_path, "plan_pending_approval", "892-plan")

        sess = self._mk_session(worktree, csid=None)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert csid_stem == f"{self._SURFACE_REF}-original"


# ---------------------------------------------------------------------------
# RFC 0005 F3 #888 — LOCAL fire-and-forget harvest (reconcile/local.py)
# ---------------------------------------------------------------------------


def _local_git_worktree(
    make_git_repo: Callable[[str], Path], name: str, *, with_commit: bool
) -> Path:
    """Build a git worktree with an origin/main ref and an optional impl commit.

    origin/main lets synthesize_git_result compute the fork point; with_commit
    controls whether the git-only synthesis yields stage_complete (commit) or
    aider_no_output (no commit).
    """
    worktree = make_git_repo(name)
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(worktree)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "fetch", "origin", "main"],
        check=True,
        capture_output=True,
    )
    if with_commit:
        (worktree / "impl.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", "impl"],
            check=True,
            capture_output=True,
        )
    return worktree


def _mk_local_session(
    sid: str,
    worktree: Path,
    liveness: LocalLivenessHandle,
    *,
    started_at: datetime | None = None,
) -> Session:
    """Build an ACTIVE, DAEMON-origin LOCAL session with a liveness handle.

    surface_ref is None (LOCAL sessions never register on the daemon roster);
    local_liveness is what harvest keys off.
    """
    return Session(
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
        surface_ref=None,
        started_at=started_at or datetime(2026, 1, 1, tzinfo=UTC),
        stage=Stage.IMPL,
        local_liveness=liveness,
    )


def test_local_harvest_dead_process_completes_and_advances(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Dead PID + commits → candidate → session COMPLETED, task advanced, event."""
    from cw.reconcile import ProposedAction

    worktree = _local_git_worktree(make_git_repo, "wt-harvest-dead", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    # A PID this high has no /proc entry → read_process_start_time_ns returns
    # None → the process reads as dead.
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    sess = _mk_local_session("harv-1", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-1",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-1",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    candidates = _detect_local_harvest_candidates(state, task_by_ticket)
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.HARVEST_LOCAL_COMPLETE
    assert candidates[0].session_id == "harv-1"

    now = datetime(2026, 1, 2, tzinfo=UTC)
    harvested = _act_on_local_harvest_candidates(
        state, candidates, now=now, task_by_ticket=task_by_ticket
    )
    assert harvested == ["harv-1"]

    reloaded = next(s for s in load_state().sessions if s.id == "harv-1")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_reason == CompletionReason.NORMAL
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "stage_complete"

    # Advanced via apply_staged_decision (IMPL→REVIEW), not reverted-as-crash.
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "harv-1")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING

    # SESSION_COMPLETED emitted with crashed=False and NO stdout key.
    events = read_events(
        consumer="test-harvest-dead",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    harvest_events = [e for e in events if e.payload.get("session_id") == "harv-1"]
    assert len(harvest_events) == 1
    assert harvest_events[0].payload.get("crashed") is False
    assert "stdout" not in harvest_events[0].payload
    assert harvest_events[0].payload.get("ticket_id") == "harv-1"


def test_local_harvest_stage_mismatch_does_not_orphan_task_or_complete_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """GitHub #1031: a stale/replayed local-harvest sentinel must not orphan
    the task or complete the session (mirrors phantom's #1019 stage-mismatch
    guard).

    ``synthesize_git_result`` always reports ``stage_reached="stage2_impl"``
    (IMPL) for a dead-process harvest with commits. If the task's row has
    already advanced past IMPL (the #986 shape) the staged-advance guard must
    refuse the route: task stays exactly as it was, session is NOT completed,
    and no SESSION_COMPLETED event fires.
    """
    from cw.reconcile import ProposedAction

    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-mismatch", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    sess = _mk_local_session("harv-mismatch", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-mismatch",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-mismatch",
                    # Row already advanced past IMPL by the time this stale
                    # dead-process harvest computes its IMPL-leg sentinel.
                    stage=Stage.REVIEW,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    candidates = _detect_local_harvest_candidates(state, task_by_ticket)
    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.HARVEST_LOCAL_COMPLETE

    now = datetime(2026, 1, 2, tzinfo=UTC)
    harvested = _act_on_local_harvest_candidates(
        state, candidates, now=now, task_by_ticket=task_by_ticket
    )
    assert harvested == []

    reloaded = next(s for s in load_state().sessions if s.id == "harv-mismatch")
    assert reloaded.status != SessionStatus.COMPLETED

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "harv-mismatch")
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.RUNNING
    assert task.disposition is None

    events = read_events(
        consumer="test-harvest-mismatch",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == "harv-mismatch" for e in events)


def test_local_harvest_live_process_not_harvested(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A live process with a matching start-time is NOT a harvest candidate."""
    from cw.local_runner import read_process_start_time_ns

    worktree = _local_git_worktree(make_git_repo, "wt-harvest-live", with_commit=True)
    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        start = read_process_start_time_ns(proc.pid)
        assert start is not None
        liveness = LocalLivenessHandle(pid=proc.pid, start_time_ns=start)
        sess = _mk_local_session("harv-live", worktree, liveness)
        state = CwState(sessions=[sess])

        candidates = _detect_local_harvest_candidates(state, {})

        assert candidates == []
    finally:
        proc.kill()
        proc.wait()


def test_local_harvest_recycled_pid_start_time_mismatch_is_dead(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """KEY: same PID but a mismatched start-time (recycled PID) reads as dead.

    Models the recycled-PID hazard: aider's PID was freed and reassigned to an
    unrelated live process. The PID exists, but its /proc start-time no longer
    matches the value captured at spawn, so the liveness pin rejects it and the
    session IS harvested. Without the start-time pin this would be a false
    "still alive" and the session would leak forever. See GitHub #888.
    """
    from cw.local_runner import read_process_start_time_ns

    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-recycled", with_commit=True
    )
    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        real_start = read_process_start_time_ns(proc.pid)
        assert real_start is not None
        # Live PID, but a start-time that does NOT match the running process.
        liveness = LocalLivenessHandle(pid=proc.pid, start_time_ns=real_start + 1)
        sess = _mk_local_session("harv-recycled", worktree, liveness)
        state = CwState(sessions=[sess])

        candidates = _detect_local_harvest_candidates(state, {})

        assert len(candidates) == 1
        assert candidates[0].session_id == "harv-recycled"
    finally:
        proc.kill()
        proc.wait()


def test_local_harvest_no_commits_synthesizes_aider_no_output(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Dead PID + no commits → git synthesis yields blocked/aider_no_output."""
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-noout", with_commit=False)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1)
    sess = _mk_local_session("harv-noout", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-noout",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-noout",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    candidates = _detect_local_harvest_candidates(state, task_by_ticket)
    assert len(candidates) == 1

    _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    reloaded = next(s for s in load_state().sessions if s.id == "harv-noout")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "blocked"
    assert reloaded.last_result["blocker"]["reason"] == "aider_no_output"


def test_act_on_local_harvest_candidates_passes_session_id_to_synthesize_git_result(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1239: the production harvest path threads candidate.session_id into
    synthesize_git_result so diagnostics land under the right session dir."""
    from cw.local_runner import synthesize_git_result as _real_synth

    worktree = _local_git_worktree(make_git_repo, "wt-harvest-sidspy", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=7)
    sess = _mk_local_session("harv-sidspy", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-sidspy",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-sidspy",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(state, task_by_ticket)
    assert len(candidates) == 1

    captured: dict[str, object] = {}

    def _spy(**kwargs: object) -> object:
        captured["session_id"] = kwargs.get("session_id")
        return _real_synth(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("cw.reconcile.local.synthesize_git_result", _spy)
    _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    assert captured["session_id"] == "harv-sidspy"


def test_local_harvest_skips_session_with_surface_ref(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A session with a surface_ref is daemon-roster tracked, not a local harvest."""
    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-surface", with_commit=True
    )
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1)
    sess = _mk_local_session("harv-surface", worktree, liveness)
    # A surface_ref means the daemon roster owns liveness; harvest must skip it.
    sess.surface_ref = "some-ref"
    state = CwState(sessions=[sess])

    assert _detect_local_harvest_candidates(state, {}) == []


def test_local_harvest_act_handles_missing_task_and_no_worktree(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Act falls back to a synthetic task when none is queued; skips no-worktree."""
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-notask", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1)
    sess = _mk_local_session("harv-notask", worktree, liveness)
    sess2 = _mk_local_session("harv-noworktree", worktree, liveness)
    sess2.worktree_path = None  # defensive-skip branch in the act phase
    state = CwState(sessions=[sess, sess2])
    save_state(state)

    # No dev-queue task for either ticket → act builds a synthetic TicketTask.
    candidates = _detect_local_harvest_candidates(state, {})
    _act_on_local_harvest_candidates(
        state, candidates, now=datetime(2026, 1, 2, tzinfo=UTC), task_by_ticket={}
    )

    reloaded = {s.id: s for s in load_state().sessions}
    assert reloaded["harv-notask"].status == SessionStatus.COMPLETED
    assert reloaded["harv-notask"].last_result is not None
    # The worktree-less candidate was skipped and left untouched.
    assert reloaded["harv-noworktree"].status == SessionStatus.ACTIVE


def test_local_harvest_fires_when_daemon_query_errors(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harvest runs BEFORE the daemon query + outage guard, so it fires anyway.

    reconcile() early-returns when `claude agents --json` errors (outage guard),
    but the local-harvest detect+act block is placed before that guard, so a dead
    LOCAL session is still completed even in a daemon outage. See GitHub #888.
    """
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-outage", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1)
    sess = _mk_local_session(
        "harv-outage", worktree, liveness, started_at=now - timedelta(seconds=60)
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-outage",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-outage",
                    stage=Stage.IMPL,
                )
            ]
        )
    )

    def _boom() -> list[dict[str, object]]:
        raise subprocess.CalledProcessError(1, ["claude", "agents", "--json"])

    def _fake_pr_merged(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        return (None, True)

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _boom)
    # Avoid a real gh call in the lockless pre-pass; treat the PR as not merged.
    monkeypatch.setattr(
        "cw.reconcile.core._deps.pr_is_merged_for_ticket", _fake_pr_merged
    )

    with freezegun.freeze_time(now):
        report = reconcile()

    reloaded = next(s for s in load_state().sessions if s.id == "harv-outage")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "stage_complete"
    assert "harv-outage" in report.completed_ticket_ids


# ---------------------------------------------------------------------------
# park_terminal_sibling_tasks
# ---------------------------------------------------------------------------


def test_park_terminal_sibling_tasks_signal_only_parks_pending(
    tmp_config_dir: Path,
) -> None:
    """PENDING task with COMPLETED sibling → BLOCKED_ON_USER under signal_only."""
    from cw.reconcile import park_terminal_sibling_tasks

    completed = TicketTask(
        ticket_id="TSB-1",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    pending = TicketTask(
        ticket_id="TSB-1",
        client="client-a",
        status=QueueItemStatus.PENDING,
        created_at=datetime(2026, 5, 2, tzinfo=UTC),  # stale: created after COMPLETED
    )
    save_dev_queue(DevQueueStore(tasks=[completed, pending]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert "TSB-1" in parked
    store = load_dev_queue()
    tasks = [t for t in store.tasks if t.ticket_id == "TSB-1"]
    statuses = {t.status for t in tasks}
    assert QueueItemStatus.PENDING not in statuses
    assert QueueItemStatus.BLOCKED_ON_USER in statuses
    blocked_task = next(t for t in tasks if t.status == QueueItemStatus.BLOCKED_ON_USER)
    assert blocked_task.disposition == ReapReason.TERMINAL_SIBLING.value


def test_park_terminal_sibling_tasks_auto_policy_cancels_pending(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PENDING task with COMPLETED sibling → CANCELLED under auto policy."""
    from cw.models import ClientConfig, LaneConfig
    from cw.reconcile import park_terminal_sibling_tasks

    completed = TicketTask(
        ticket_id="TSB-AUTO",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    pending = TicketTask(
        ticket_id="TSB-AUTO",
        client="client-a",
        status=QueueItemStatus.PENDING,
        created_at=datetime(2026, 5, 2, tzinfo=UTC),  # stale: created after COMPLETED
    )
    save_dev_queue(DevQueueStore(tasks=[completed, pending]))
    save_state(CwState(sessions=[]))

    # Patch load_clients to return an auto-policy lane config.
    auto_client = ClientConfig(
        name="client-a",
        workspace_path=Path("/tmp/ws"),
        lanes=[LaneConfig(name="default", max_parallel=1, reap_policy=ReapPolicy.AUTO)],
    )
    monkeypatch.setattr(
        "cw.reconcile.tasks.load_clients", lambda: {"client-a": auto_client}
    )

    parked = park_terminal_sibling_tasks()

    assert "TSB-AUTO" in parked
    store = load_dev_queue()
    parked_task = next(
        t
        for t in store.tasks
        if t.ticket_id == "TSB-AUTO" and t.status != QueueItemStatus.COMPLETED
    )
    assert parked_task.status == QueueItemStatus.CANCELLED


def test_park_terminal_sibling_tasks_cancelled_sibling_also_parks(
    tmp_config_dir: Path,
) -> None:
    """Stale PENDING newer than CANCELLED sibling → parked (CANCELLED is terminal)."""
    from cw.reconcile import park_terminal_sibling_tasks

    # Explicit timestamps: stale PENDING is NEWER than the CANCELLED row —
    # this is the enqueue-dedup-gap pattern, not a doctor's collapse.
    cancelled = TicketTask(
        ticket_id="TSB-CXL",
        client="client-a",
        status=QueueItemStatus.CANCELLED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    pending = TicketTask(
        ticket_id="TSB-CXL",
        client="client-a",
        status=QueueItemStatus.PENDING,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    save_dev_queue(DevQueueStore(tasks=[cancelled, pending]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert "TSB-CXL" in parked
    store = load_dev_queue()
    tasks = [t for t in store.tasks if t.ticket_id == "TSB-CXL"]
    statuses = {t.status for t in tasks}
    assert QueueItemStatus.PENDING not in statuses


def test_park_terminal_sibling_tasks_ordering_guard_skips_doctor_collapse(
    tmp_config_dir: Path,
) -> None:
    """PENDING older than CANCELLED siblings → skip (doctor's collapse pattern)."""
    from cw.reconcile import park_terminal_sibling_tasks

    # Doctor's _collapse_blocked_on_user_tasks pattern:
    # oldest BLOCKED_ON_USER → PENDING, newer ones → CANCELLED.
    # The PENDING is the live re-dispatch; newer CANCELLEDs are dedup artifacts.
    pending = TicketTask(
        ticket_id="TSB-ORD",
        client="client-a",
        status=QueueItemStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),  # oldest
    )
    cancelled1 = TicketTask(
        ticket_id="TSB-ORD",
        client="client-a",
        status=QueueItemStatus.CANCELLED,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),  # newer
    )
    cancelled2 = TicketTask(
        ticket_id="TSB-ORD",
        client="client-a",
        status=QueueItemStatus.CANCELLED,
        created_at=datetime(2026, 1, 3, tzinfo=UTC),  # newest
    )
    save_dev_queue(DevQueueStore(tasks=[pending, cancelled1, cancelled2]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert parked == []
    store = load_dev_queue()
    t = next(
        t
        for t in store.tasks
        if t.ticket_id == "TSB-ORD" and t.status == QueueItemStatus.PENDING
    )
    assert t.status == QueueItemStatus.PENDING  # untouched


def test_park_terminal_sibling_tasks_no_sibling_noop(
    tmp_config_dir: Path,
) -> None:
    """PENDING task with no terminal sibling → no change."""
    from cw.reconcile import park_terminal_sibling_tasks

    pending = TicketTask(
        ticket_id="TSB-NONE",
        client="client-a",
        status=QueueItemStatus.PENDING,
    )
    save_dev_queue(DevQueueStore(tasks=[pending]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert parked == []
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TSB-NONE")
    assert t.status == QueueItemStatus.PENDING


def test_park_terminal_sibling_tasks_different_client_noop(
    tmp_config_dir: Path,
) -> None:
    """COMPLETED row for different client does not affect the PENDING row."""
    from cw.reconcile import park_terminal_sibling_tasks

    completed_other = TicketTask(
        ticket_id="TSB-X",
        client="client-b",
        status=QueueItemStatus.COMPLETED,
    )
    pending = TicketTask(
        ticket_id="TSB-X",
        client="client-a",
        status=QueueItemStatus.PENDING,
    )
    save_dev_queue(DevQueueStore(tasks=[completed_other, pending]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert parked == []
    store = load_dev_queue()
    t = next(
        t for t in store.tasks if t.ticket_id == "TSB-X" and t.client == "client-a"
    )
    assert t.status == QueueItemStatus.PENDING


def test_park_terminal_sibling_tasks_emits_reap_proposed_event(
    tmp_config_dir: Path,
) -> None:
    """Parks emit SESSION_REAP_PROPOSED(reason='terminal_sibling')."""
    from cw.reconcile import park_terminal_sibling_tasks

    completed = TicketTask(
        ticket_id="TSB-EVT",
        client="client-a",
        status=QueueItemStatus.COMPLETED,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    pending = TicketTask(
        ticket_id="TSB-EVT",
        client="client-a",
        status=QueueItemStatus.PENDING,
        created_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    save_dev_queue(DevQueueStore(tasks=[completed, pending]))
    save_state(CwState(sessions=[]))

    park_terminal_sibling_tasks()

    events = read_events()
    reap_events = [
        e
        for e in events
        if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        and e.payload.get("ticket_id") == "TSB-EVT"
    ]
    assert len(reap_events) == 1
    assert reap_events[0].payload["reason"] == ReapReason.TERMINAL_SIBLING.value
    assert reap_events[0].payload["proposed_action"] == "terminal_sibling"


def test_park_terminal_sibling_tasks_failed_not_terminal(
    tmp_config_dir: Path,
) -> None:
    """FAILED sibling does not trigger parking; COMPLETED/CANCELLED are terminal."""
    from cw.reconcile import park_terminal_sibling_tasks

    failed = TicketTask(
        ticket_id="TSB-FAIL",
        client="client-a",
        status=QueueItemStatus.FAILED,
    )
    pending = TicketTask(
        ticket_id="TSB-FAIL",
        client="client-a",
        status=QueueItemStatus.PENDING,
    )
    save_dev_queue(DevQueueStore(tasks=[failed, pending]))
    save_state(CwState(sessions=[]))

    parked = park_terminal_sibling_tasks()

    assert parked == []
    store = load_dev_queue()
    t = next(
        t
        for t in store.tasks
        if t.ticket_id == "TSB-FAIL" and t.status == QueueItemStatus.PENDING
    )
    assert t.status == QueueItemStatus.PENDING


# ---------------------------------------------------------------------------
# main-checkout drift detection — #940 (worktree worker escaped to main)
# ---------------------------------------------------------------------------


class TestConciergeAndEscalationWiring:
    """RFC 0008 capstone (#1015): wiring-only — both new sweeps run exactly
    once per reconcile() tick, in both the no-phantoms and phantom branches
    of _reconcile_locked."""

    def test_no_phantoms_branch_calls_both_sweeps_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        concierge_mock = MagicMock(return_value=[])
        escalation_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_concierge_recoveries", concierge_mock
        )
        monkeypatch.setattr("cw.reconcile.core.run_escalation_sweep", escalation_mock)

        reconcile()

        concierge_mock.assert_called_once()
        escalation_mock.assert_called_once()

    def test_phantom_branch_calls_both_sweeps_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A phantom (missing-surface) DAEMON session routes through the
        phantom-handling tail of _reconcile_locked — the sweeps must still
        fire exactly once there too."""
        state = CwState(sessions=[_mk_session("phantom-1", "missing-ref")])
        save_state(state)
        # A non-empty (but unrelated) roster keeps `native_live` non-empty so
        # _looks_like_daemon_outage's "roster looks totally dead" guard
        # doesn't short-circuit the tick before reaching either sweep.
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "unrelated1"}],
        )
        concierge_mock = MagicMock(return_value=[])
        escalation_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_concierge_recoveries", concierge_mock
        )
        monkeypatch.setattr("cw.reconcile.core.run_escalation_sweep", escalation_mock)

        report = reconcile()

        assert report.phantom_session_ids == ["phantom-1"]
        concierge_mock.assert_called_once()
        escalation_mock.assert_called_once()


def _write_gate_orchestrator_yaml(*, gate_recipes_enabled: bool) -> None:
    """Write orchestrator.yaml toggling the gate-recipe master switch.

    concierge_enabled stays False so the tick exercises only the gate-recipe
    path (single-concern), and reconcile()'s real load_orchestrator_config()
    reads this file off disk rather than an in-memory _config() object.
    """
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"gate_recipes_enabled: {str(gate_recipes_enabled).lower()}\n"
        "concierge_enabled: false\n"
    )


def _stub_gate_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub cw.gh._sp.run so the post-approve comment never hits real gh."""

    def _fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("cw.gh._sp.run", _fake_run)


class TestReconcileGateRecipeIntegration:
    """RFC 0009 (#1088 item 2): behavioral reconcile()-level coverage of the
    gate-recipe act path — the real load_orchestrator_config() disk read,
    per-lane enablement, gate advance, and event emission through ONE unmocked
    tick. The sibling TestConciergeAndEscalationWiring is mocked wiring-only
    (asserts the sweeps are *called*); this drives the reactor for real.

    The owning session is refless (surface_ref=None) so compute_drift skips it
    (no phantom → the cheap no-phantoms branch of _reconcile_locked, which still
    runs run_gate_recipes via _run_terminal_backstops_and_sweeps).
    """

    @staticmethod
    def _blocked_task(stage: Stage) -> TicketTask:
        return TicketTask(
            ticket_id="GEN-1",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=stage,
            session_id="sess-1",
        )

    def test_clean_review_auto_approved_advances_to_finalize(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.FINALIZE
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_APPROVE_REVIEW

    def test_clean_plan_auto_adopted_advances_to_impl(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        stub_fetch_plan(monkeypatch, plan_body())
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.PLAN)]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN

    def test_master_switch_off_leaves_task_blocked(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=False)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        # Recipe never fired: the row stays parked at the review gate. (Do not
        # assert escalation_parked_at — the escalation sweep may stamp it in the
        # same tick; only the gate-recipe non-fire is under test here.)
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert events == []
