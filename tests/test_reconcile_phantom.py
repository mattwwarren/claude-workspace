"""Unit tests for cw.reconcile.phantom.

Phantom (dead-surface) sweep: detect/act candidates, crash-complete vs
sentinel-salvage routing, dirty/clean worktree routing, and phantom_reverted
events.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    SPAWN_GRACE_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _has_terminal_sentinel,
    reconcile,
)
from cw.reconcile._shared import _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _mk_headless_daemon_session,
    _mk_phantom_daemon_session,
    _mk_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _state_queue_snapshot,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)


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
# dirty-worktree push-notification storm regression tests (GitHub #763)
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
        now=started_at,
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
        now=started_at,
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
        now=started_at,
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
        now=started_at,
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
        now=started_at,
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

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, now=started_at
    )

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

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, now=started_at
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.usage_limit_detected is False


def _ul_record(text: str, timestamp: str) -> dict[str, object]:
    """One timestamped assistant text record for _write_transcript_records."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_detect_phantom_candidates_usage_limit_recent_at_tail_true(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345: a limit message at the transcript tail is recent → detected True."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-ul-recent"
    sess = _mk_phantom_daemon_session(
        "phantom-ul-recent",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record("working on it", "2026-01-01T00:00:10+00:00"),
            _ul_record(
                "You've hit your session limit · resets 5am",
                "2026-01-01T00:00:20+00:00",
            ),
        ],
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, now=started_at
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.usage_limit_detected is True


def test_detect_phantom_candidates_usage_limit_stale_false(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345: an early limit message with later unrelated work is stale → False."""
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-ul-stale"
    sess = _mk_phantom_daemon_session(
        "phantom-ul-stale",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(
                "You've hit your session limit · resets 5am",
                "2026-01-01T00:00:10+00:00",
            ),
            # 301s after the match — beyond the 300s backoff window.
            _ul_record("unrelated later progress", "2026-01-01T00:05:11+00:00"),
        ],
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, now=started_at
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.CRASH_COMPLETE
    assert c.usage_limit_detected is False


def test_phantom_sentinel_mismatch_veto_when_transcript_live(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #1281: an already_refused phantom whose transcript is still
    actively advancing is vetoed instead of falling straight through to
    CRASH_COMPLETE -- the #1281 incident killed a session 56 seconds before
    its valid AUTO_DEV_RESULT landed.
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-sentinel-veto-live"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = started_at + timedelta(minutes=5)

    sess = _mk_phantom_daemon_session(
        "phantom-veto-live-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    sess.last_result = {"paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON}
    payload = _stage_complete_payload()
    payload["ticket_id"] = "phantom-veto-live-1"
    transcript = _write_salvage_transcript(
        home, worktree, "csid-phantom-veto-live", payload
    )
    # 30 seconds stale -- well under TRANSCRIPT_LIVENESS_WINDOW_SECONDS (300s).
    fresh_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(str(transcript), (fresh_ts, fresh_ts))

    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(state, phantom_set={sess.id}, now=now)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.proposed_action == ProposedAction.SENTINEL_STAGE_MISMATCH_VETOED
    assert c.stale_minutes is not None
    assert c.stale_minutes < 5
    assert not any(
        cand.proposed_action == ProposedAction.CRASH_COMPLETE for cand in candidates
    )


def test_phantom_sentinel_mismatch_veto_falls_through_when_transcript_stale(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript stale beyond TRANSCRIPT_LIVENESS_WINDOW_SECONDS does not
    veto the crash -- preserves the pre-#1281 CRASH_COMPLETE fall-through
    once the grace window has elapsed (GitHub #1281).
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-sentinel-veto-stale"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    sess = _mk_phantom_daemon_session(
        "phantom-veto-stale-1",
        started_at,
        surface_ref="fake-short-id",
        worktree_path=worktree,
    )
    sess.last_result = {"paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON}
    payload = _stage_complete_payload()
    payload["ticket_id"] = "phantom-veto-stale-1"
    transcript = _write_salvage_transcript(
        home, worktree, "csid-phantom-veto-stale", payload
    )
    stale_ts = (started_at + timedelta(minutes=1)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    now = started_at + timedelta(seconds=TRANSCRIPT_LIVENESS_WINDOW_SECONDS + 3600)

    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(state, phantom_set={sess.id}, now=now)

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.CRASH_COMPLETE


def test_phantom_sentinel_mismatch_veto_falls_through_when_no_transcript(
    tmp_config_dir: Path,
) -> None:
    """An already_refused phantom with no locatable transcript falls through
    to CRASH_COMPLETE (fail-toward-crash), mirroring
    ``_liveness_veto_candidate``'s unlocatable-transcript contract (GitHub #1281).
    """
    from cw.reconcile import ProposedAction, _detect_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-veto-notranscript-1", started_at)
    sess.last_result = {"paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON}
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    candidates = _detect_phantom_candidates(
        state, phantom_set={sess.id}, now=started_at
    )

    assert len(candidates) == 1
    assert candidates[0].proposed_action == ProposedAction.CRASH_COMPLETE


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
    transcript = _write_salvage_transcript(
        home, worktree, "csid-phantom-refusal", payload
    )
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
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-refusal-1": task},
        now=started_at,
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

    # #1281: the already_refused fall-through is now gated on transcript
    # liveness -- backdate the transcript beyond the liveness window so this
    # second pass exercises the pre-#1281 CRASH_COMPLETE fall-through (the
    # "eventually crashes once the transcript goes quiet" branch), not the
    # new veto.
    stale_ts = (started_at + timedelta(minutes=1)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    now_2 = started_at + timedelta(hours=1)

    # Second detect pass over the now-marked session: the already_refused
    # skip check stops offering the same doomed advance candidate -- it
    # falls through to CRASH_COMPLETE instead.
    candidates_2 = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-refusal-1": task_after},
        now=now_2,
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
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-nt-1": task},
        now=started_at,
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
    transcript = _write_salvage_transcript(
        home, worktree, "csid-phantom-preserve", payload
    )
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
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-preserve-1": task},
        now=started_at,
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

    # #1281: backdate the transcript beyond the liveness window so this
    # second pass exercises the pre-#1281 CRASH_COMPLETE fall-through, not
    # the new veto.
    stale_ts = (started_at + timedelta(minutes=1)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    now_2 = started_at + timedelta(hours=1)

    # Second detect pass over the now-marked session: already_refused must
    # still stop offering the same doomed advance candidate even though the
    # marker's paused_status value is idle.py's, not the #1149 refusal
    # reason -- it falls through to CRASH_COMPLETE instead.
    candidates_2 = _detect_phantom_candidates(
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-preserve-1": task},
        now=now_2,
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
        state,
        phantom_set={sess.id},
        task_by_ticket={"phantom-later-1": task},
        now=started_at,
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


def test_act_on_phantom_salvage_completion_routes_queue_and_emits_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SALVAGE_COMPLETION phantom → session COMPLETED, queue COMPLETED, event."""
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


def test_act_on_phantom_sentinel_mismatch_veto_emits_event_no_mutation(
    tmp_config_dir: Path,
) -> None:
    """SENTINEL_STAGE_MISMATCH_VETOED candidate emits the vetoed event only.

    session.sentinel_stage_mismatch_vetoed fires; zero queue/session mutation
    accompanies it (GitHub #1281).
    """
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_phantom_candidates

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    sess = _mk_phantom_daemon_session("phantom-veto-act-1", started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="phantom-veto-act-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="phantom-veto-act-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="phantom-veto-act-1",
        proposed_action=ProposedAction.SENTINEL_STAGE_MISMATCH_VETOED,
        ticket_id="phantom-veto-act-1",
        client="client-a",
        stale_minutes=4.2,
    )

    _act_on_phantom_candidates(state, [candidate], now=now)

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "phantom-veto-act-1")
    assert t.status == QueueItemStatus.RUNNING
    assert t.disposition is None

    s = next(s for s in state.sessions if s.id == "phantom-veto-act-1")
    assert s.status == SessionStatus.ACTIVE

    events = read_events(
        consumer="test-phantom-veto-act-1",
        event_types=[OrchestratorEventType.SESSION_SENTINEL_STAGE_MISMATCH_VETOED],
    )
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ticket_id"] == "phantom-veto-act-1"
    assert payload["client"] == "client-a"
    assert payload["session_id"] == "phantom-veto-act-1"
    assert payload["stale_minutes"] == 4.2


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
