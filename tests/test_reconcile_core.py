"""Unit tests for cw.reconcile.core — reconcile() / _reconcile_locked orchestration."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from cw._lock_guard import LockRank, is_rank_held
from cw.auto_dev_result import AutoDevResult
from cw.config import (
    load_clients,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    ReconcileReport,
    _verify_supervisor_session_id,
    codex_boot,
    reconcile,
    revert_timed_out_tasks,
)
from cw.reconcile import core as reconcile_core
from cw.reconcile._shared import ProposedAction, ReapCandidate
from cw.reconcile.codex_boot import CAPTURE_BUDGET_SECONDS, CleanProbe, CleanProbes
from cw.reconcile.deferred import DeferredReconcileJobs
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    DirtyChecks,
)
from cw.reconcile.gate_plan_probes import (
    PLAN_PREFETCH_BUDGET_SECONDS,
    PLAN_PREFETCH_MAX_PER_TICK,
    PlanProbes,
)
from cw.reconcile.harvest_synthesis import HARVEST_CAPTURE_BUDGET_SECONDS, HarvestFacts
from cw.reconcile.review_recipes import (
    RECIPE_ADDRESS_REVIEW,
    RECIPE_AUTO_FIX_CI,
    RECIPE_REQUEST_REVIEWER,
    DeferredReviewDispatch,
    _shared,
)
from cw.reconcile.review_recipes._shared import RepoSlugs
from cw.review_strategy import ReviewStrategy
from tests._clients_yaml import ClientSpec, staged_client, write_clients_yaml
from tests._dirty_check_helpers import DIRTY_CHECKS_LOGGER, unavailable_records
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _attention_events,
    _auto_config,
    _mk_headless_daemon_session,
    _mk_phantom_daemon_session,
    _mk_routed_session,
    _mk_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _stamp_transcript_age,
    _ul_record,
    _write_idle_transcript_with_text,
    _write_transcript_records,
    probe_sessions_lock_free,
)
from tests.conftest import CapturedEvent, _make_daemon_session, _make_ticket_task
from tests.test_pr_hydrate import _pr_state
from tests.test_reconcile_codex_reparks import _seed_two_client_parks
from tests.test_reconcile_gate_recipes import _GATE_LANES, _clean_result, _plan_result
from tests.test_reconcile_gate_recipes import _make_session as _gate_session
from tests.test_reconcile_gate_recipes import _make_task as _gate_task
from tests.test_reconcile_review_recipes import _cr_task


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

    # Hermetic gh: a missing gh binary would route the ticket gh_blocked
    # instead of exercising the phantom revert under test.
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
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


def test_reconcile_prepass_uses_fresh_merged_pr_state_without_gh_call(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: a phantom DAEMON session whose task carries a fresh
    MERGED pr_state is treated as merged (completed_reason=NORMAL, not
    CRASHED) WITHOUT any _deps.pr_is_merged_for_ticket call."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-MERGED"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-MERGED",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="MERGED", hydrated_at=datetime.now(UTC) - timedelta(seconds=10)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.NORMAL


def test_reconcile_prepass_uses_fresh_open_pr_state_without_gh_call(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: fresh OPEN pr_state -> not merged, and no gh call is made."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-OPEN"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-OPEN",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="OPEN", hydrated_at=datetime.now(UTC) - timedelta(seconds=10)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.CRASHED


def test_reconcile_prepass_falls_back_to_gh_when_pr_state_stale(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: stale pr_state falls back to the ordinary gh call path
    unchanged (existing pre-#975 behavior)."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-STALE"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-STALE",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="MERGED", hydrated_at=datetime.now(UTC) - timedelta(seconds=300)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    calls: list[str] = []

    def _capture(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        calls.append(_tid)
        return True, True

    monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    assert calls == ["TKT-STALE"]
    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.completed_reason == CompletionReason.NORMAL


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
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
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

    home = Path.home()

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

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    # Non-empty live set bypasses outage guard; surface_ref not present → phantom.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    report = reconcile()

    assert report.usage_limited is True
    assert "phantom-ul-reconcile" in report.reverted_ticket_ids


def test_reconcile_usage_limited_false_from_incomplete_phantom_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed tail cannot provide positive phantom usage-limit evidence."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    home = Path.home()

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-phantom-ul-incomplete"
    surface_ref = "dead-ul-i"
    sess = _mk_phantom_daemon_session(
        "phantom-ul-incomplete",
        started_at,
        surface_ref=surface_ref,
        worktree_path=worktree,
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="phantom-ul-incomplete",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="phantom-ul-incomplete",
                )
            ]
        )
    )

    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record(
                "You've hit your session limit · resets 3:40am",
                "2026-01-01T00:00:20+00:00",
            )
        ],
        filename=f"{surface_ref}-incomplete.jsonl",
    )
    transcript.write_text(transcript.read_text() + "{ malformed json\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )

    report = reconcile()

    assert report.usage_limited is False


def _setup_stalled_ul_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: list[dict[str, object]],
    slug: str,
) -> None:
    """Wire up a headless daemon session whose ancient started_at trips the
    wall-clock watchdog, with a real timestamped transcript for the recency gate."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)

    worktree = tmp_path / f"wt-{slug}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session(slug, worktree, started_at)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=slug,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=slug,
                    attempts=1,  # below cap -> normal REVERT_TASK path
                )
            ]
        )
    )

    transcript = _write_transcript_records(Path.home(), worktree, records)
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


def test_watchdog_usage_limited_true_when_limit_message_at_tail(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345: a limit message at the transcript tail → report.usage_limited True."""
    _setup_stalled_ul_session(
        tmp_path,
        monkeypatch,
        records=[
            _ul_record("working through the plan", "2026-01-01T00:00:10+00:00"),
            _ul_record(
                "You've hit your session limit · resets 3:40am",
                "2026-01-01T00:00:20+00:00",
            ),
        ],
        slug="watchdog-ul-recent",
    )

    report = reconcile()

    assert report.usage_limited is True
    assert "watchdog-ul-recent" in report.reverted_ticket_ids


def test_watchdog_usage_limited_false_when_limit_message_stale_and_reap_unrelated(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345: an early limit message with later unrelated work is stale →
    report.usage_limited False even though the session is still reaped."""
    _setup_stalled_ul_session(
        tmp_path,
        monkeypatch,
        records=[
            _ul_record(
                "You've hit your session limit · resets 3:40am",
                "2026-01-01T00:00:10+00:00",
            ),
            # 301s after the match — beyond the 300s backoff window.
            _ul_record("unrelated later progress", "2026-01-01T00:05:11+00:00"),
        ],
        slug="watchdog-ul-stale",
    )

    report = reconcile()

    assert report.usage_limited is False
    assert "watchdog-ul-stale" in report.reverted_ticket_ids


def test_watchdog_usage_limited_true_when_timestamp_missing(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1345: a limit message with no parseable timestamp has no recency anchor
    → the backoff site's fail_open=True default still arms report.usage_limited."""
    _setup_stalled_ul_session(
        tmp_path,
        monkeypatch,
        records=[_ul_record("You've hit your session limit · resets 3:40am")],
        slug="watchdog-ul-nots",
    )

    report = reconcile()

    assert report.usage_limited is True
    assert "watchdog-ul-nots" in report.reverted_ticket_ids


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


def test_reconcile_sweeps_a_leaked_daemon_worker(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2480: reconcile() stops a roster worker whose session already
    COMPLETED, before the daemon-outage guard -- and regardless of it, since
    this sweep reads the roster directly rather than ``claude agents --json``."""
    from cw.events import OrchestratorEventType

    daemon = FakeNativeDaemonClient()
    short_id = daemon.seed_live_worker(Path("/tmp/leaked-wt"))
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

    completed = _make_daemon_session(
        id="done0001",
        name="client-a/auto-dev/GEN-99",
        status=SessionStatus.COMPLETED,
        surface_ref=short_id,
    )
    save_state(CwState(sessions=[completed]))

    reconcile()

    assert daemon.stop_calls == [short_id]
    events = read_events(
        consumer="test_reconcile_sweeps_leaked_worker",
        event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
    )
    assert len(events) == 1
    assert events[0].payload["short_id"] == short_id
    assert events[0].payload["ticket_id"] == "GEN-99"


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


class TestVerifySupervisorSessionId:
    """_verify_supervisor_session_id compares stored csid against supervisor state."""

    def _mk_daemon_session(
        self,
        sid: str,
        surface_ref: str | None,
        claude_session_id: str | None,
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> Session:
        return _make_daemon_session(
            id=sid,
            name=f"client-a/auto-dev/{sid}",
            status=status,
            worktree_path=None,
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


# reconcile()'s dev-queue loads: its gh pre-pass first, then the codex
# clean-probe pre-pass (#2563), then the gate recipes' plan prefetch
# pre-pass third (#2545). The review recipes' repo-slug pre-pass (#2564) loads
# through its own module's binding, so it is not counted here; the dirty-check
# pre-pass (#2548) is the fourth, and the local harvest-facts pre-pass (#2565)
# the fifth, last before the lock.
_CODEX_PRE_PASS_LOAD = 2
_PLAN_PRE_PASS_LOAD = 3
_DIRTY_PRE_PASS_LOAD = 4
_HARVEST_PRE_PASS_LOAD = 5


class TestCodexLiveWriterRepark:
    """#2307: wiring-only — the live-writer codex-orphan re-evaluation runs
    exactly once per reconcile() tick, in both branches of _reconcile_locked,
    scoped to the very clients mapping that tick reconciles (review round 1:
    never a second, independent client load). Its behavior under the held
    sessions_lock is covered end to end in
    tests/test_reconcile_codex_reparks.py."""

    @staticmethod
    def _spy_load_clients(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[dict[str, ClientConfig]]:
        """Record every clients mapping reconcile() loads, delegating."""
        loaded: list[dict[str, ClientConfig]] = []
        real = reconcile_core.load_clients

        def _spy() -> dict[str, ClientConfig]:
            clients = real()
            loaded.append(clients)
            return clients

        monkeypatch.setattr(reconcile_core, "load_clients", _spy)
        return loaded

    def test_no_phantoms_branch_calls_the_sweep_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        loaded = self._spy_load_clients(monkeypatch)
        repark_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_codex_live_writer_reparks", repark_mock
        )

        reconcile()

        repark_mock.assert_called_once()
        assert set(repark_mock.call_args.kwargs) == {
            "now",
            "config",
            "clients",
            "probes",
        }
        assert len(loaded) == 1
        assert repark_mock.call_args.kwargs["clients"] is loaded[0]

    def test_phantom_branch_calls_the_sweep_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CwState(sessions=[_mk_session("phantom-1", "missing-ref")])
        save_state(state)
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "unrelated1"}],
        )
        loaded = self._spy_load_clients(monkeypatch)
        repark_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_codex_live_writer_reparks", repark_mock
        )

        report = reconcile()

        assert report.phantom_session_ids == ["phantom-1"]
        repark_mock.assert_called_once()
        assert len(loaded) == 1
        assert repark_mock.call_args.kwargs["clients"] is loaded[0]

    def test_locked_tick_passes_its_own_client_scope_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A _reconcile_locked call handed a clients mapping scopes the sweep
        to exactly that mapping, and reads clients.yaml no second time."""
        save_state(CwState(sessions=[]))
        loaded = self._spy_load_clients(monkeypatch)
        repark_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_codex_live_writer_reparks", repark_mock
        )
        scope = {
            "client-a": ClientConfig(name="client-a", workspace_path=Path("/tmp/ws"))
        }

        with sessions_lock():
            reconcile_core._reconcile_locked(
                clients=scope, deferred=DeferredReconcileJobs()
            )

        repark_mock.assert_called_once()
        assert repark_mock.call_args.kwargs["clients"] is scope
        assert loaded == []
        # No codex_probes handed in: the sweep gets None, an always-miss
        # source, so every git-gated candidate defers (#2563).
        assert repark_mock.call_args.kwargs["probes"] is None

    def test_pre_pass_probes_are_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2563: reconcile() captures the codex clean probes before it takes
        sessions_lock, and hands that one object to both in-lock consumers."""
        save_state(CwState(sessions=[]))
        sentinel = CleanProbes()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(
            *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
        ) -> CleanProbes:
            del config, clients
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(
            reconcile_core, "_capture_codex_clean_probes", _fake_capture
        )
        repark_mock = MagicMock(return_value=[])
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_codex_live_writer_reparks", repark_mock
        )
        monkeypatch.setattr(
            "cw.reconcile.core._act_on_local_harvest_candidates", harvest_mock
        )

        reconcile()

        assert lock_free_at_capture == [True]
        assert repark_mock.call_args.kwargs["probes"] is sentinel
        assert harvest_mock.call_args.kwargs["codex_probes"] is sentinel

    def test_unreadable_state_in_the_pre_pass_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2563 containment: reconcile() also serves cw status/list/start, so
        a pre-pass read failure must not fail it. Every codex candidate gets an
        empty CleanProbes (a miss) and defers."""
        save_state(CwState(sessions=[]))
        real_load = reconcile_core.load_dev_queue
        loads: list[int] = []

        def _second_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _CODEX_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(reconcile_core, "load_dev_queue", _second_load_fails)
        repark_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.run_codex_live_writer_reparks", repark_mock
        )

        with caplog.at_level(logging.WARNING, logger=reconcile_core.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        probes = repark_mock.call_args.kwargs["probes"]
        assert isinstance(probes, CleanProbes)
        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert "codex clean-probe pre-pass could not read state" in caplog.text


def _validation_error() -> ValidationError:
    try:
        CwState.model_validate({"sessions": 5})
    except ValidationError as err:
        return err
    msg = "expected a ValidationError"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    "error",
    [
        OSError("state unreadable"),
        json.JSONDecodeError("bad", "{", 0),
        _validation_error(),
    ],
    ids=["os-error", "json-decode-error", "validation-error"],
)
def test_capture_pre_pass_contains_state_read_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def _fail() -> CwState:
        raise error

    monkeypatch.setattr(reconcile_core, "load_state", _fail)

    probes = reconcile_core._capture_codex_clean_probes(
        config=OrchestratorConfig(), clients={}
    )

    assert probes.captured_keys == frozenset()
    assert probes.budget_seconds is None


def test_capture_pre_pass_does_not_swallow_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broad except: only the expected read errors are contained."""

    def _fail() -> CwState:
        msg = "a bug, not an unreadable file"
        raise RuntimeError(msg)

    monkeypatch.setattr(reconcile_core, "load_state", _fail)

    with pytest.raises(RuntimeError, match="a bug"):
        reconcile_core._capture_codex_clean_probes(
            config=OrchestratorConfig(), clients={}
        )


def test_capture_pre_pass_spends_at_most_its_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-pass runs within CAPTURE_BUDGET_SECONDS: once a slow capture
    spends it, later candidates get no probe (they defer in-lock)."""
    _seed_two_client_parks(
        tmp_config_dir,
        tmp_path,
        make_git_repo,
        lane_policies={"client-a": ReapPolicy.AUTO, "client-b": ReapPolicy.AUTO},
    )
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: [])
    clock = {"now": 0.0}
    monkeypatch.setattr(codex_boot, "monotonic", lambda: clock["now"])
    real_probe = codex_boot.probe_clean_state

    def _slow_probe(*args: Any) -> CleanProbe:
        probe = real_probe(*args)
        clock["now"] += CAPTURE_BUDGET_SECONDS + 1
        return probe

    monkeypatch.setattr(codex_boot, "probe_clean_state", _slow_probe)

    probes = reconcile_core._capture_codex_clean_probes(
        config=OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
        clients=load_clients(),
    )

    assert probes.budget_seconds == CAPTURE_BUDGET_SECONDS
    assert len(probes.captured_keys) == 1


class TestPlanPrefetchPrePass:
    """#2545: the gate recipes' plan-of-record read runs in a lockless
    pre-pass, and the in-lock run_gate_recipes only reads its probes."""

    @pytest.mark.parametrize("phantom", [False, True], ids=["no-phantoms", "phantom"])
    def test_plan_pre_pass_is_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch, phantom: bool
    ) -> None:
        if phantom:
            save_state(CwState(sessions=[_mk_session("phantom-1", "missing-ref")]))
            monkeypatch.setattr(
                "cw.reconcile.core._claude_agents_json",
                lambda: [{"sessionId": "unrelated1"}],
            )
        else:
            save_state(CwState(sessions=[]))
        sentinel = PlanProbes()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(
            *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
        ) -> PlanProbes:
            del config, clients
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(reconcile_core, "_capture_plan_probes", _fake_capture)
        gate_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.core.run_gate_recipes", gate_mock)

        report = reconcile()

        assert report.phantom_session_ids == (["phantom-1"] if phantom else [])
        assert lock_free_at_capture == [True]
        gate_mock.assert_called_once()
        assert gate_mock.call_args.kwargs["plan_probes"] is sentinel

    def test_plan_pre_pass_is_bounded_by_the_explicit_cap_and_budget(self) -> None:
        save_state(CwState(sessions=[]))

        probes = reconcile_core._capture_plan_probes(
            config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
        )

        assert probes.max_captures == PLAN_PREFETCH_MAX_PER_TICK
        assert probes.budget_seconds == PLAN_PREFETCH_BUDGET_SECONDS

    @pytest.mark.parametrize(
        "error",
        [
            OSError("state unreadable"),
            json.JSONDecodeError("bad", "{", 0),
            _validation_error(),
        ],
        ids=["os-error", "json-decode-error", "validation-error"],
    )
    def test_capture_plan_pre_pass_contains_state_read_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        def _fail() -> CwState:
            raise error

        monkeypatch.setattr(reconcile_core, "load_state", _fail)

        with caplog.at_level(logging.WARNING, logger=reconcile_core.__name__):
            probes = reconcile_core._capture_plan_probes(
                config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
            )

        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert probes.max_captures is None
        assert "plan prefetch pre-pass could not read state" in caplog.text

    def test_capture_plan_pre_pass_does_not_swallow_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only state/dev-queue read errors are contained: a reader bug
        propagates out of the pre-pass, as it did from the old in-lock read."""
        save_dev_queue(DevQueueStore(tasks=[_gate_task(stage=Stage.PLAN)]))
        save_state(CwState(sessions=[_gate_session(last_result=_plan_result())]))

        def _boom(_task: TicketTask, _client_cfg: ClientConfig | None) -> str | None:
            msg = "a reader bug, not an unreadable file"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._plan_of_record_body", _boom)

        with pytest.raises(RuntimeError, match="a reader bug"):
            reconcile_core._capture_plan_probes(
                config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
            )

    def test_capture_plan_pre_pass_reads_nothing_when_gate_recipes_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[str] = []

        def _spy() -> CwState:
            reads.append("state")
            return CwState(sessions=[])

        monkeypatch.setattr(reconcile_core, "load_state", _spy)

        probes = reconcile_core._capture_plan_probes(
            config=OrchestratorConfig(gate_recipes_enabled=False), clients={}
        )

        assert reads == []
        assert probes.captures == 0
        assert probes.max_captures is None

    def test_unreadable_dev_queue_in_the_plan_pre_pass_does_not_fail_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = reconcile_core.load_dev_queue
        loads: list[int] = []

        def _third_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _PLAN_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(reconcile_core, "load_dev_queue", _third_load_fails)
        gate_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.core.run_gate_recipes", gate_mock)

        with caplog.at_level(logging.WARNING, logger=reconcile_core.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        probes = gate_mock.call_args.kwargs["plan_probes"]
        assert isinstance(probes, PlanProbes)
        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert "plan prefetch pre-pass could not read state" in caplog.text


class TestHarvestFactsPrePass:
    """#2565: the local harvest's git facts are captured in reconcile()'s last
    lockless pre-pass, and the in-lock act only looks them up."""

    def test_harvest_facts_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        sentinel = HarvestFacts()
        lock_free_at_capture: list[bool] = []

        def _fake_capture() -> HarvestFacts:
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(reconcile_core, "_capture_harvest_facts", _fake_capture)
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core._act_on_local_harvest_candidates", harvest_mock
        )

        reconcile()

        assert lock_free_at_capture == [True]
        assert harvest_mock.call_args.kwargs["harvest_facts"] is sentinel

    def test_harvest_capture_runs_last_before_the_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its facts age from capture, so no other pre-pass may run after it."""
        save_state(CwState(sessions=[]))
        order: list[str] = []

        def _step(name: str, result: object) -> Callable[..., object]:
            def _record(*_args: object, **_kwargs: object) -> object:
                order.append(name)
                return result

            return _record

        monkeypatch.setattr(
            reconcile_core, "_capture_codex_clean_probes", _step("codex", None)
        )
        monkeypatch.setattr(
            reconcile_core, "capture_review_repo_slugs", _step("slugs", None)
        )
        monkeypatch.setattr(
            reconcile_core, "_capture_dirty_checks", _step("dirty", None)
        )
        monkeypatch.setattr(
            reconcile_core, "_capture_harvest_facts", _step("harvest", None)
        )
        monkeypatch.setattr(
            reconcile_core, "_act_on_local_harvest_candidates", _step("locked", [])
        )

        reconcile()

        assert order == ["codex", "slugs", "dirty", "harvest", "locked"]

    def test_unreadable_state_in_harvest_pre_pass_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = reconcile_core.load_dev_queue
        loads: list[int] = []

        def _harvest_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _HARVEST_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(reconcile_core, "load_dev_queue", _harvest_load_fails)
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core._act_on_local_harvest_candidates", harvest_mock
        )

        with caplog.at_level(logging.WARNING, logger=reconcile_core.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        facts = harvest_mock.call_args.kwargs["harvest_facts"]
        assert isinstance(facts, HarvestFacts)
        assert facts.budget_seconds is None
        assert "harvest-facts pre-pass could not read state" in caplog.text

    def test_capture_harvest_pre_pass_does_not_swallow_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> CwState:
            msg = "a bug, not an unreadable file"
            raise RuntimeError(msg)

        monkeypatch.setattr(reconcile_core, "load_state", _fail)

        with pytest.raises(RuntimeError, match="a bug"):
            reconcile_core._capture_harvest_facts()

    def test_capture_harvest_pre_pass_builds_a_budgeted_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        driver = MagicMock()
        monkeypatch.setattr(reconcile_core, "capture_local_harvest_facts", driver)

        facts = reconcile_core._capture_harvest_facts()

        assert facts.budget_seconds == HARVEST_CAPTURE_BUDGET_SECONDS
        assert driver.call_args.kwargs["facts"] is facts

    def test_reconcile_locked_without_harvest_facts_passes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core._act_on_local_harvest_candidates", harvest_mock
        )

        with sessions_lock():
            reconcile_core._reconcile_locked(deferred=DeferredReconcileJobs())

        assert harvest_mock.call_args.kwargs["harvest_facts"] is None


class TestFixDispatchRunsPostLock:
    """#2064: run_fix_dispatch's spawn reaches spawn_create_impl's own
    sessions_lock() acquisition, so it must run strictly AFTER reconcile()'s
    own sessions_lock hold releases -- not from inside
    _run_terminal_backstops_and_sweeps, which runs while the lock is held."""

    def test_fix_dispatch_wiring_no_phantoms_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))

        def _spy(*, config: OrchestratorConfig) -> list[str]:
            with sessions_lock():
                pass
            return []

        fix_dispatch_mock = MagicMock(side_effect=_spy)
        monkeypatch.setattr("cw.reconcile.core.run_fix_dispatch", fix_dispatch_mock)

        reconcile()

        fix_dispatch_mock.assert_called_once()

    def test_fix_dispatch_wiring_phantom_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A phantom (missing-surface) DAEMON session routes through the
        phantom-handling tail of _reconcile_locked -- the hoisted call must
        still run post-lock there too."""
        state = CwState(sessions=[_mk_session("phantom-1", "missing-ref")])
        save_state(state)
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "unrelated1"}],
        )

        def _spy(*, config: OrchestratorConfig) -> list[str]:
            with sessions_lock():
                pass
            return []

        fix_dispatch_mock = MagicMock(side_effect=_spy)
        monkeypatch.setattr("cw.reconcile.core.run_fix_dispatch", fix_dispatch_mock)

        report = reconcile()

        assert report.phantom_session_ids == ["phantom-1"]
        fix_dispatch_mock.assert_called_once()

    def test_fix_dispatch_runs_before_completed_ticket_ids_early_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards against placing the hoisted call after the
        ``completed_ticket_ids`` early return -- it must run unconditionally
        every tick, including ticks with no completions."""
        save_state(CwState(sessions=[]))
        save_dev_queue(DevQueueStore(tasks=[]))
        fix_dispatch_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.core.run_fix_dispatch", fix_dispatch_mock)

        reconcile()

        fix_dispatch_mock.assert_called_once()


def _sessions_lock_held() -> bool:
    """Whether this thread holds ``sessions_lock()`` (the guard's held stack)."""
    return is_rank_held(LockRank.SESSIONS)


class TestReviewRecipeDispatchRunsPostLock:
    """#1229: the address_review spawn and the auto_fix_ci dispatch tick both
    re-acquire sessions_lock(), so they must run AFTER reconcile()'s own hold
    releases. Before the deferral they ran inside _reconcile_locked, hit
    CwLockReentrancyError, and the recipe's ``except CwError`` swallowed it
    -- address_review never spawned."""

    @staticmethod
    def _enable_recipes(monkeypatch: pytest.MonkeyPatch) -> None:
        """Turn the review-recipe master switch on for reconcile()'s own config
        loads (the pre-pass and _reconcile_locked both read it via core)."""
        monkeypatch.setattr(
            "cw.reconcile.core.load_orchestrator_config",
            lambda: OrchestratorConfig(review_recipes_enabled=True),
        )

    @staticmethod
    def _seed_address_review_row(tmp_config_dir: Path, worktree: Path) -> TicketTask:
        write_clients_yaml(ClientSpec("acme", tmp_config_dir, default_branch="main"))
        task = _cr_task(
            review_recipes={RECIPE_ADDRESS_REVIEW: True}, worktree_path=worktree
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    @staticmethod
    def _seed_auto_fix_ci_row(
        tmp_config_dir: Path, status: QueueItemStatus | None = None
    ) -> TicketTask:
        write_clients_yaml(ClientSpec("acme", tmp_config_dir, default_branch="main"))
        extra: dict[str, Any] = {} if status is None else {"status": status}
        task = _cr_task(
            review_recipes={RECIPE_AUTO_FIX_CI: True},
            pr_state=_pr_state(
                state="OPEN", attention_state="ci_failing", failing_checks=["lint"]
            ),
            **extra,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        return task

    def test_address_review_really_spawns_under_a_real_reconcile(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (#1229): through the REAL spawn_create_impl (fake daemon)
        and a REAL sessions_lock held by reconcile(), the /address-review
        worker is spawned and registered. On the unfixed tree spawn_create_impl's
        own ``with sessions_lock():`` raised CwLockReentrancyError after the
        daemon spawn, the recipe swallowed it into a PR_ACTION_FAILED, and no
        session row was ever persisted."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-spawn"))
        self._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        reconcile(dispatch_review_jobs=True)

        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]
        sessions = load_state().sessions
        assert [s.name for s in sessions] == ["acme/address-review-42"]
        assert sessions[0].surface_ref == "00000001"
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert [e.correlation_id for e in taken] == ["GEN-1"]
        assert load_dev_queue().tasks[0].address_review_fired_at is not None

    def test_address_review_spawns_in_the_phantom_branch_too(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A phantom session routes through the tail of _reconcile_locked, the
        other _run_terminal_backstops_and_sweeps call site: the sink must be
        threaded into that branch as well."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-phantom"))
        self._enable_recipes(monkeypatch)
        save_state(CwState(sessions=[_mk_session("phantom-1", "missing-ref")]))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "unrelated1"}],
        )
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        report = reconcile(dispatch_review_jobs=True)

        assert report.phantom_session_ids == ["phantom-1"]
        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]

    def test_dispatch_runs_with_sessions_lock_released(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The spawn executes only after the lock is down: the thread-local held
        flag is clear when the job runs, and was still set while the recipe
        prepared it (so the job was genuinely deferred, not never-locked)."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-lock"))
        self._enable_recipes(monkeypatch)
        held_at_prepare: list[bool] = []
        held_at_spawn: list[bool] = []
        real_prepare = reconcile_core.run_review_recipes

        def _prepare_spy(
            *,
            config: OrchestratorConfig,
            jobs: DeferredReconcileJobs,
            repo_slugs: RepoSlugs | None = None,
        ) -> list[str]:
            result = real_prepare(config=config, jobs=jobs, repo_slugs=repo_slugs)
            held_at_prepare.append(_sessions_lock_held())
            return result

        def _spawn_spy(**kwargs: Any) -> str:
            held_at_spawn.append(_sessions_lock_held())
            return "spawned-session-id"

        monkeypatch.setattr("cw.reconcile.core.run_review_recipes", _prepare_spy)
        monkeypatch.setattr("cw.spawn.spawn_create_impl", _spawn_spy)

        reconcile(dispatch_review_jobs=True)

        assert held_at_prepare == [True]
        assert held_at_spawn == [False]

    def test_auto_fix_ci_requeues_the_row_without_a_nested_dispatch_tick(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1229: the auto_fix_ci job requeues its terminal row in place and
        stops. The live loop's reconcile() already holds dispatch_loop_lock, so a
        nested run_dispatch_loop(once=True) could only raise
        DispatchLoopLockedError and misreport the requeue as a failure; the loop
        picks the row up on its own next tick."""
        self._seed_auto_fix_ci_row(tmp_config_dir, status=QueueItemStatus.COMPLETED)
        self._enable_recipes(monkeypatch)

        def _no_tick(**_kwargs: Any) -> None:
            pytest.fail("auto_fix_ci must not run a nested dispatch tick")

        monkeypatch.setattr("cw.dispatch.run_dispatch_loop", _no_tick)

        reconcile(dispatch_review_jobs=True)

        row = load_dev_queue().tasks[0]
        assert row.status is QueueItemStatus.PENDING
        assert row.auto_fix_ci_fired_at is not None
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert [e.correlation_id for e in taken] == ["GEN-1"]

    def test_failing_job_is_logged_and_does_not_break_the_pass(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A deferred job whose spawn raises leaves the same trail the inline
        path did (warning log + PR_ACTION_FAILED), reconcile() still returns
        its report, and the post-lock steps after it still run."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-fail"))
        self._enable_recipes(monkeypatch)

        def _boom(**_kwargs: Any) -> str:
            msg = "daemon refused the spawn"
            raise CwError(msg)

        monkeypatch.setattr("cw.spawn.spawn_create_impl", _boom)
        fix_dispatch_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.core.run_fix_dispatch", fix_dispatch_mock)

        with caplog.at_level("WARNING", logger="cw.reconcile.review_recipes"):
            report = reconcile(dispatch_review_jobs=True)

        assert isinstance(report, ReconcileReport)
        fix_dispatch_mock.assert_called_once()
        assert "review_recipe_dispatch_failed ticket=GEN-1" in caplog.text
        failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
        assert [e.correlation_id for e in failed] == ["GEN-1"]
        assert "daemon refused the spawn" in json.dumps(failed[0].payload)

    @pytest.mark.parametrize("completions", [[], ["GEN-9"]], ids=["none", "some"])
    def test_review_dispatch_runs_before_the_other_post_lock_steps(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        completions: list[str],
    ) -> None:
        """The post-lock drain runs first -- queued stops (#1232), then the
        review dispatch -- before complete_timed_out_merged_tasks and
        run_fix_dispatch, whether or not the post-pass finds merged-timed-out
        completions. The review latch is already stamped, so a job lost to an
        exception in a later step would never retry this episode
        (run_fix_dispatch, by contrast, re-detects every tick)."""
        order: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile.core.run_post_lock_jobs",
            lambda _jobs: order.append("post_lock_drain"),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.dispatch_deferred_review_jobs",
            lambda _sink: order.append("review_dispatch"),
        )
        monkeypatch.setattr(
            "cw.reconcile.core.complete_timed_out_merged_tasks",
            lambda: order.append("complete_merged") or completions,
        )
        monkeypatch.setattr(
            "cw.reconcile.core.run_fix_dispatch",
            lambda **_kw: order.append("fix_dispatch") or [],
        )

        report = reconcile(dispatch_review_jobs=True)

        assert order == [
            "post_lock_drain",
            "review_dispatch",
            "complete_merged",
            "fix_dispatch",
        ]
        assert report.completed_ticket_ids == completions

    def test_daemon_outage_early_return_defers_nothing(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outage guard returns before the recipes run: the sink reaches the
        post-lock dispatch empty."""
        monkeypatch.setattr(
            "cw.reconcile.core._looks_like_daemon_outage", lambda *_a, **_k: True
        )
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.reconcile.core.dispatch_deferred_review_jobs", calls.append
        )

        reconcile(dispatch_review_jobs=True)

        assert calls == [DeferredReviewDispatch()]

    def test_the_sink_is_not_created_or_dispatched_without_the_flag(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[Any] = []
        monkeypatch.setattr(
            "cw.reconcile.core.dispatch_deferred_review_jobs", calls.append
        )

        reconcile()

        assert calls == []

    # --- two-pass latch semantics at reconcile() level -------------------

    def test_second_pass_after_a_successful_spawn_does_nothing_more(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The one-shot latch holds across real reconcile() passes: the second
        pass spawns nothing and emits no second PR_ACTION_TAKEN."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-two-pass"))
        self._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        reconcile(dispatch_review_jobs=True)
        reconcile(dispatch_review_jobs=True)

        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert [e.correlation_id for e in taken] == ["GEN-1"]
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []

    def test_second_pass_after_a_failing_job_does_not_retry(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed job leaves the latch stamped (PR_ACTION_FAILED is the visible
        signal); the next real pass neither retries the spawn nor re-emits."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-fail-twice"))
        self._enable_recipes(monkeypatch)
        attempts: list[str] = []

        def _boom(**kwargs: Any) -> str:
            attempts.append(kwargs["prompt"])
            msg = "daemon refused the spawn"
            raise CwError(msg)

        monkeypatch.setattr("cw.spawn.spawn_create_impl", _boom)

        reconcile(dispatch_review_jobs=True)
        assert load_dev_queue().tasks[0].address_review_fired_at is not None
        reconcile(dispatch_review_jobs=True)

        assert attempts == ["/address-review 42"]
        taken = read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN])
        assert [e.correlation_id for e in taken] == ["GEN-1"]
        failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
        assert [e.correlation_id for e in failed] == ["GEN-1"]

    # --- item 1: the caller-owned sink survives a later in-lock failure ---

    @pytest.mark.parametrize(
        "failing_step",
        [
            "cw.reconcile.review_recipes.core._act_auto_fix_ci",
            "cw.reconcile.review_recipes.core._act_request_reviewer",
            "cw.reconcile.review_recipes.core._act_escalate_merge_block",
            "cw.reconcile.core.run_escalation_sweep",
        ],
        ids=["next_recipe", "request_reviewer", "escalate_merge_block", "sweep"],
    )
    def test_job_is_dispatched_even_when_a_later_in_lock_step_raises(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
        failing_step: str,
    ) -> None:
        """address_review's job is prepared (latch stamped, PR_ACTION_TAKEN
        emitted) before a later step raises inside the lock. The job lives in the
        caller-owned sink, so it is STILL dispatched from reconcile()'s finally
        -- and the original exception still propagates."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-lost-job"))
        self._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        def _boom(*_args: object, **_kwargs: object) -> list[str]:
            msg = "later in-lock step exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(failing_step, _boom)

        with pytest.raises(RuntimeError, match="later in-lock step exploded"):
            reconcile(dispatch_review_jobs=True)

        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]
        assert [s.name for s in load_state().sessions] == ["acme/address-review-42"]
        assert load_dev_queue().tasks[0].address_review_fired_at is not None
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []

    def test_a_dispatch_failure_in_the_finally_does_not_mask_the_original_error(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The locked body raises AND the deferred job's spawn raises a non-CwError:
        the caller sees the LOCKED BODY's exception (per-job isolation keeps the
        finally from replacing it), and the job's failure is still recorded."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-mask"))
        self._enable_recipes(monkeypatch)

        def _spawn_oserror(**_kwargs: Any) -> str:
            msg = "disk on fire"
            raise OSError(msg)

        def _sweep_boom(**_kwargs: Any) -> None:
            msg = "original in-flight failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.spawn.spawn_create_impl", _spawn_oserror)
        monkeypatch.setattr("cw.reconcile.core.run_escalation_sweep", _sweep_boom)

        with pytest.raises(RuntimeError, match="original in-flight failure"):
            reconcile(dispatch_review_jobs=True)

        failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
        assert [e.correlation_id for e in failed] == ["GEN-1"]
        assert "OSError: disk on fire" in json.dumps(failed[0].payload)

    def test_an_unwritable_failure_record_neither_skips_siblings_nor_masks_the_error(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The job's PR_ACTION_FAILED record is itself an inbox write that can fail
        with the same fault (disk full). That second failure is logged, not raised:
        the sibling job still dispatches and the locked body's original exception
        is the one the caller sees."""
        write_clients_yaml(ClientSpec("acme", tmp_config_dir, default_branch="main"))
        tasks = [
            _cr_task(
                ticket_id=ticket_id,
                pr_url=f"https://github.com/acme/widgets/pull/{number}",
                review_recipes={RECIPE_ADDRESS_REVIEW: True},
                worktree_path=make_git_repo(f"emit-{ticket_id}"),
            )
            for ticket_id, number in (("GEN-1", 41), ("GEN-2", 42))
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        self._enable_recipes(monkeypatch)
        spawned: list[str] = []

        def _spawn(**kwargs: Any) -> str:
            if kwargs["prompt"].endswith(" 41"):
                msg = "no space left on device"
                raise OSError(msg)
            spawned.append(kwargs["prompt"])
            return "spawned-session-id"

        def _emit_boom(*_args: object, **_kwargs: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        def _sweep_boom(**_kwargs: Any) -> None:
            msg = "original in-flight failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.spawn.spawn_create_impl", _spawn)
        monkeypatch.setattr(
            "cw.reconcile.review_recipes.core._emit_pr_action_failed", _emit_boom
        )
        monkeypatch.setattr("cw.reconcile.core.run_escalation_sweep", _sweep_boom)

        with (
            caplog.at_level("ERROR", logger="cw.reconcile.review_recipes"),
            pytest.raises(RuntimeError, match="original in-flight failure"),
        ):
            reconcile(dispatch_review_jobs=True)

        assert spawned == ["/address-review 42"]
        assert "review_recipe_dispatch_crashed ticket=GEN-1" in caplog.text
        assert "review_recipe_pr_action_failed_emit_crashed ticket=GEN-1" in caplog.text

    # --- item 2: per-job isolation of ANY exception ----------------------

    def test_non_cwerror_job_failure_is_isolated_and_recorded(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A job raising a non-CwError (OSError) is logged with its traceback and
        recorded as PR_ACTION_FAILED; the sibling job still dispatches, and
        complete_timed_out_merged_tasks / run_fix_dispatch still run."""
        write_clients_yaml(ClientSpec("acme", tmp_config_dir, default_branch="main"))
        tasks = [
            _cr_task(
                ticket_id=ticket_id,
                pr_url=f"https://github.com/acme/widgets/pull/{number}",
                review_recipes={RECIPE_ADDRESS_REVIEW: True},
                worktree_path=make_git_repo(f"iso-{ticket_id}"),
            )
            for ticket_id, number in (("GEN-1", 41), ("GEN-2", 42))
        ]
        save_dev_queue(DevQueueStore(tasks=tasks))
        self._enable_recipes(monkeypatch)
        spawned: list[str] = []

        def _spawn(**kwargs: Any) -> str:
            if kwargs["prompt"].endswith(" 41"):
                msg = "no space left on device"
                raise OSError(msg)
            spawned.append(kwargs["prompt"])
            return "spawned-session-id"

        monkeypatch.setattr("cw.spawn.spawn_create_impl", _spawn)
        completed_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.core.complete_timed_out_merged_tasks", completed_mock
        )
        fix_dispatch_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.core.run_fix_dispatch", fix_dispatch_mock)

        with caplog.at_level("ERROR", logger="cw.reconcile.review_recipes"):
            report = reconcile(dispatch_review_jobs=True)

        assert isinstance(report, ReconcileReport)
        assert spawned == ["/address-review 42"]
        assert "review_recipe_dispatch_crashed ticket=GEN-1" in caplog.text
        assert "no space left on device" in caplog.text
        failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
        assert [e.correlation_id for e in failed] == ["GEN-1"]
        assert "OSError: no space left on device" in json.dumps(failed[0].payload)
        completed_mock.assert_called_once()
        fix_dispatch_mock.assert_called_once()

    # --- item 3: only the live dispatch loop dispatches ------------------

    def test_reconcile_without_the_flag_neither_stamps_nor_emits_nor_spawns(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cw status/list/start/doctor path (no flag): address_review and
        auto_fix_ci do not run at all -- no latch, no PR_ACTION_TAKEN, no spawn,
        no requeue -- while request_reviewer and escalate_merge_block do."""
        write_clients_yaml(ClientSpec("acme", tmp_config_dir, default_branch="main"))
        ar_task = _cr_task(
            ticket_id="GEN-1",
            review_recipes={RECIPE_ADDRESS_REVIEW: True},
            worktree_path=make_git_repo("ar-no-flag"),
        )
        ci_task = _cr_task(
            ticket_id="GEN-2",
            review_recipes={RECIPE_AUTO_FIX_CI: True},
            status=QueueItemStatus.COMPLETED,
            pr_state=_pr_state(
                state="OPEN", attention_state="ci_failing", failing_checks=["lint"]
            ),
        )
        save_dev_queue(DevQueueStore(tasks=[ar_task, ci_task]))
        self._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)
        request_reviewer_mock = MagicMock(return_value=[])
        escalate_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.review_recipes.core._act_request_reviewer",
            request_reviewer_mock,
        )
        monkeypatch.setattr(
            "cw.reconcile.review_recipes.core._act_escalate_merge_block", escalate_mock
        )

        reconcile()

        assert daemon.spawn_calls == []
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []
        rows = {t.ticket_id: t for t in load_dev_queue().tasks}
        assert rows["GEN-1"].address_review_fired_at is None
        assert rows["GEN-2"].auto_fix_ci_fired_at is None
        assert rows["GEN-2"].status is QueueItemStatus.COMPLETED
        request_reviewer_mock.assert_called_once()
        escalate_mock.assert_called_once()

    def test_the_latch_survives_a_read_only_pass_for_the_loop_to_fire_later(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The point of not burning the latch: an operator's ``cw status`` pass
        (no flag) leaves the recipe armed, and the next dispatch-loop pass fires
        it exactly once."""
        self._seed_address_review_row(tmp_config_dir, make_git_repo("ar-armed"))
        self._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        reconcile()  # cw status / cw list / cw start / cw doctor
        assert daemon.spawn_calls == []

        reconcile(dispatch_review_jobs=True)  # the dispatch loop's tick

        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]


# --- GitHub #2564: review-recipe repo slugs resolved before sessions_lock ----


def _spy_slug_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Path, bool]]:
    """Wrap the pre-pass's real slug resolver; record (git_dir, lock_held)."""
    calls: list[tuple[Path, bool]] = []
    real_resolve = _shared._resolve_repo_slug

    def _spy(git_dir: Path) -> str | None:
        calls.append((git_dir, _sessions_lock_held()))
        return real_resolve(git_dir)

    monkeypatch.setattr("cw.reconcile.review_recipes._shared._resolve_repo_slug", _spy)
    return calls


def _set_origin(repo: Path, url: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", url],
        capture_output=True,
        check=True,
    )


_PostLock = TestReviewRecipeDispatchRunsPostLock


class TestReviewRepoSlugsResolvedBeforeTheLock:
    """#2564: the address_review / auto_fix_ci cross-repo guard reads each
    candidate's origin slug from a pre-pass ``reconcile()`` runs before it
    takes ``sessions_lock``; nothing under the lock runs ``git``."""

    @staticmethod
    def _seed_both_rows(worktree: Path, workspace: Path) -> None:
        write_clients_yaml(ClientSpec("acme", workspace, default_branch="main"))
        ar_task = _cr_task(
            ticket_id="GEN-1",
            review_recipes={RECIPE_ADDRESS_REVIEW: True},
            worktree_path=worktree,
        )
        ci_task = _cr_task(
            ticket_id="GEN-2",
            review_recipes={RECIPE_AUTO_FIX_CI: True},
            status=QueueItemStatus.COMPLETED,
            pr_state=_pr_state(
                state="OPEN", attention_state="ci_failing", failing_checks=["lint"]
            ),
        )
        save_dev_queue(DevQueueStore(tasks=[ar_task, ci_task]))

    def test_every_slug_resolve_runs_with_the_lock_free(
        self,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = make_git_repo("slug-ar")
        workspace = make_git_repo("slug-ws")
        self._seed_both_rows(worktree, workspace)
        _PostLock._enable_recipes(monkeypatch)
        calls = _spy_slug_resolver(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        reconcile(dispatch_review_jobs=True)

        assert {git_dir for git_dir, _held in calls} == {worktree, workspace}
        assert all(held is False for _git_dir, held in calls)
        assert [prompt for _cwd, prompt in daemon.spawn_calls] == ["/address-review 42"]
        rows = {t.ticket_id: t for t in load_dev_queue().tasks}
        assert rows["GEN-1"].address_review_fired_at is not None
        assert rows["GEN-2"].auto_fix_ci_fired_at is not None
        assert rows["GEN-2"].status is QueueItemStatus.PENDING
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []

    def test_a_mismatched_origin_still_trips_the_guard_end_to_end(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = make_git_repo("slug-mismatch")
        _set_origin(worktree, "https://github.com/other/repo.git")
        _PostLock._seed_address_review_row(tmp_config_dir, worktree)
        _PostLock._enable_recipes(monkeypatch)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        reconcile(dispatch_review_jobs=True)

        assert daemon.spawn_calls == []
        assert read_events(event_types=[OrchestratorEventType.PR_ACTION_TAKEN]) == []
        failed = read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED])
        assert [e.correlation_id for e in failed] == ["GEN-1"]
        assert "repo mismatch" in str(failed[0].payload["error"])
        assert load_dev_queue().tasks[0].address_review_fired_at is None

    def test_a_non_dispatching_reconcile_resolves_no_slug(
        self,
        tmp_config_dir: Path,
        make_git_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _PostLock._seed_address_review_row(
            tmp_config_dir, make_git_repo("slug-readonly")
        )
        _PostLock._enable_recipes(monkeypatch)
        calls = _spy_slug_resolver(monkeypatch)

        reconcile()

        assert calls == []

    @pytest.mark.parametrize("phantom", [False, True], ids=["no_phantom", "phantom"])
    def test_the_captured_slugs_are_threaded_to_the_recipes(
        self, monkeypatch: pytest.MonkeyPatch, *, phantom: bool
    ) -> None:
        """Both ``_run_terminal_backstops_and_sweeps`` call sites hand the
        recipes the one object the pre-lock capture returned."""
        save_dev_queue(DevQueueStore(tasks=[]))
        if phantom:
            save_state(CwState(sessions=[_mk_session("phantom-1", "missing-ref")]))
            monkeypatch.setattr(
                "cw.reconcile.core._claude_agents_json",
                lambda: [{"sessionId": "unrelated1"}],
            )
        else:
            save_state(CwState(sessions=[]))
        sentinel = RepoSlugs()
        captured_with: list[tuple[bool, bool]] = []

        def _fake_capture(
            *, config: OrchestratorConfig, dispatching: bool
        ) -> RepoSlugs:
            del config
            captured_with.append((dispatching, probe_sessions_lock_free()))
            return sentinel

        recipes_mock = MagicMock(return_value=[])
        monkeypatch.setattr(reconcile_core, "capture_review_repo_slugs", _fake_capture)
        monkeypatch.setattr("cw.reconcile.core.run_review_recipes", recipes_mock)

        report = reconcile(dispatch_review_jobs=True)

        assert report.phantom_session_ids == (["phantom-1"] if phantom else [])
        assert captured_with == [(True, True)]
        recipes_mock.assert_called_once()
        assert recipes_mock.call_args.kwargs["repo_slugs"] is sentinel


# --- GitHub #1762: session-id-namespace advisory sweep ------------------------


def _advisory_sweep(
    state: CwState, live: set[str], *, now: datetime | None = None
) -> dict[str, str | None]:
    """Run the advisory sweep and return {ticket_id: advisory_note}."""
    from cw.reconcile._shared import _stamp_session_id_mismatch_advisories

    _stamp_session_id_mismatch_advisories(state, live, now=now)
    return {t.ticket_id: t.advisory_note for t in load_dev_queue().tasks}


def test_stamp_advisory_flags_unresolvable_session_id(tmp_config_dir: Path) -> None:
    """#1762: a RUNNING row whose session_id resolves to nothing is flagged.

    This is the shape operators reported on #1738/#1774 — a row pointing at an
    id that matches no session, mistaken for cw "losing" the worker when it is
    really three different id namespaces being compared as one.
    """
    from cw.reconcile._shared import _SESSION_ID_MISMATCH_ADVISORY_NOTE

    state = CwState(sessions=[_mk_session("live-1", surface_ref="aaaaaaaa")])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-ghost",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="a2fe4bd5",
                )
            ]
        )
    )

    notes = _advisory_sweep(state, {"aaaaaaaa"})
    assert notes["T-ghost"] == _SESSION_ID_MISMATCH_ADVISORY_NOTE


def test_stamp_advisory_leaves_a_live_running_row_alone(tmp_config_dir: Path) -> None:
    """#1762: a resolvable, roster-live RUNNING row is never flagged."""
    sess = _mk_session("live-2", surface_ref="bbbbbbbb")
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-live",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sess.id,
                )
            ]
        )
    )

    notes = _advisory_sweep(state, {"bbbbbbbb"})
    assert notes["T-live"] is None


def test_stamp_advisory_flags_a_roster_absent_running_row(
    tmp_config_dir: Path,
) -> None:
    """#1762: a resolvable session that fell out of the roster is flagged too."""
    from cw.reconcile._shared import _SESSION_ID_MISMATCH_ADVISORY_NOTE

    sess = _mk_session("gone-1", surface_ref="cccccccc")
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-gone",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sess.id,
                )
            ]
        )
    )

    notes = _advisory_sweep(state, {"dddddddd"})
    assert notes["T-gone"] == _SESSION_ID_MISMATCH_ADVISORY_NOTE


def test_stamp_advisory_clears_a_stale_note_when_the_row_recovers(
    tmp_config_dir: Path,
) -> None:
    """#1762: the note is re-derived every tick, not latched.

    A row whose session comes back into the roster must lose the advisory on the
    very next sweep — otherwise the REASON column would accumulate stale flags
    an operator has no way to dismiss.
    """
    sess = _mk_session("flap-1", surface_ref="eeeeeeee")
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-flap",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sess.id,
                )
            ]
        )
    )

    assert _advisory_sweep(state, set())["T-flap"] is not None
    assert _advisory_sweep(state, {"eeeeeeee"})["T-flap"] is None


def test_stamp_advisory_ignores_non_running_and_sessionless_rows(
    tmp_config_dir: Path,
) -> None:
    """#1762: only RUNNING rows that claim a session are in scope.

    A parked row's REASON column belongs to ``blocked_reason``, and a row with
    no session_id has no binding to be mismatched.
    """
    state = CwState(sessions=[])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-parked",
                    client="client-a",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id="a2fe4bd5",
                ),
                TicketTask(
                    ticket_id="T-pending",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                ),
            ]
        )
    )

    notes = _advisory_sweep(state, set())
    assert notes == {"T-parked": None, "T-pending": None}


def test_stamp_advisory_respects_the_spawn_grace_window(tmp_config_dir: Path) -> None:
    """#1762: a session too young to have registered is not flagged.

    Same allowance ``compute_drift`` makes before calling a surface phantom —
    without it every freshly dispatched row would flash ``?session_mismatch``
    in the REASON column for the first SPAWN_GRACE_SECONDS of its life.
    """
    from cw.reconcile import SPAWN_GRACE_SECONDS

    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    sess = _mk_session(
        "fresh-1", surface_ref="ffffffff", started_at=now - timedelta(seconds=5)
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-fresh",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=sess.id,
                )
            ]
        )
    )

    # Inside the grace window: absent from the roster, but not yet a mismatch.
    assert _advisory_sweep(state, set(), now=now)["T-fresh"] is None
    # Past it: the same absence now is.
    later = now + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)
    assert _advisory_sweep(state, set(), now=later)["T-fresh"] is not None


# ---------------------------------------------------------------------------
# #2324: mid-turn usage-limit sweep wiring
# ---------------------------------------------------------------------------

_MID_TURN_SURFACE = "live2324"
# Real layout: the roster reports the full id and the transcript is named by it;
# reconcile backfills it as claude_session_id, which locate_transcript then uses.
_MID_TURN_FULL_ID = _MID_TURN_SURFACE + "-0000-4000-8000-000000000000"
_MID_TURN_TICKET = "mid-2324"
_PHANTOM_TICKET = "ph-2324"


def _seed_mid_turn_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: OrchestratorConfig,
    with_phantom: bool,
) -> None:
    """One roster-present worker stopped on a usage limit, plus an optional phantom."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", lambda: config)
    home = Path.home()
    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": _MID_TURN_FULL_ID}],
    )

    started_at = datetime.now(UTC) - timedelta(hours=1)
    worktree = tmp_path / "wt-mid-2324"
    sessions = [
        _mk_headless_daemon_session(
            _MID_TURN_TICKET, worktree, started_at, surface_ref=_MID_TURN_SURFACE
        )
    ]
    tasks = [
        _make_ticket_task(
            ticket_id=_MID_TURN_TICKET,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=_MID_TURN_TICKET,
        )
    ]
    if with_phantom:
        sessions.append(_mk_phantom_daemon_session(_PHANTOM_TICKET, started_at))
        tasks.append(
            _make_ticket_task(
                ticket_id=_PHANTOM_TICKET,
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id=_PHANTOM_TICKET,
            )
        )
    save_state(CwState(sessions=sessions))
    save_dev_queue(DevQueueStore(tasks=tasks))

    transcript = _write_transcript_records(
        home,
        worktree,
        [
            _ul_record("working on it", "2026-09-24T00:20:00+00:00"),
            _ul_record(
                "You've hit your weekly limit · resets Sep 26, 11pm (America/New_York)",
                "2026-09-24T00:50:00+00:00",
            ),
            {"type": "cost-state", "timestamp": "2026-09-24T00:50:01+00:00"},
        ],
        filename=f"{_MID_TURN_FULL_ID}.jsonl",
    )
    now_ts = datetime.now(UTC).timestamp()
    os.utime(str(transcript), (now_ts, now_ts))


@pytest.mark.parametrize("with_phantom", [False, True], ids=["no-phantom", "phantom"])
def test_reconcile_auto_reverts_mid_turn_usage_limited_ticket(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_phantom: bool,
) -> None:
    """Under reap_policy: auto the mid-turn park lands in reverted_ticket_ids on
    both of _reconcile_locked's all_reverted branches (#2324)."""
    _seed_mid_turn_limit(
        tmp_path, monkeypatch, config=_auto_config(), with_phantom=with_phantom
    )

    report = reconcile()

    assert _MID_TURN_TICKET in report.reverted_ticket_ids
    if with_phantom:
        assert _PHANTOM_TICKET in report.reverted_ticket_ids
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == _MID_TURN_TICKET)
    assert task.status is QueueItemStatus.PENDING
    assert task.next_eligible_at is not None


def test_reconcile_signal_only_parks_mid_turn_ticket_without_reverting(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the signal_only default the row is parked, not reverted (#2324)."""
    _seed_mid_turn_limit(
        tmp_path, monkeypatch, config=OrchestratorConfig(), with_phantom=False
    )

    report = reconcile()

    assert _MID_TURN_TICKET not in report.reverted_ticket_ids
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "usage_limited_mid_turn"


def _fail_mid_turn_dev_queue_save(monkeypatch: pytest.MonkeyPatch, *, nth: int) -> None:
    """The sweep's *nth* dev-queue write raises; the others land.

    Its writes are, in order: the decision (1), the audit mark (2) and the
    final transition (3).
    """
    real_save = save_dev_queue
    saves: list[int] = []

    def _save_failing_once(store: DevQueueStore) -> None:
        saves.append(1)
        if len(saves) == nth:
            msg = "dev-queue write failed"
            raise OSError(msg)
        real_save(store)

    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.save_dev_queue", _save_failing_once
    )


def test_reconcile_contains_a_failed_mid_turn_decision_and_retries_it(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed intent write decides nothing and does not abort the tick.

    The next reconcile decides and finishes the act -- PENDING, no attempt
    charged (#2324).
    """
    _seed_mid_turn_limit(
        tmp_path, monkeypatch, config=_auto_config(), with_phantom=False
    )
    before = load_dev_queue().tasks[0].unproductive_attempts
    _fail_mid_turn_dev_queue_save(monkeypatch, nth=1)

    assert _MID_TURN_TICKET not in reconcile().reverted_ticket_ids
    assert load_state().sessions[0].status is SessionStatus.ACTIVE
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.usage_limit_act is None

    report = reconcile()

    assert _MID_TURN_TICKET in report.reverted_ticket_ids
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.unproductive_attempts == before
    assert task.next_eligible_at is not None
    assert load_state().sessions[0].reap_reason is ReapReason.USAGE_LIMIT_MID_TURN


def _assert_act_still_in_flight(*, unproductive_attempts: int) -> None:
    """The row is RUNNING under its intent and was charged nothing."""
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == _MID_TURN_TICKET
    assert task.usage_limit_act is not None
    assert task.unproductive_attempts == unproductive_attempts


def _assert_act_finished_uncharged(
    report: ReconcileReport, *, unproductive_attempts: int
) -> None:
    assert _MID_TURN_TICKET in report.reverted_ticket_ids
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.usage_limit_act is None
    assert task.unproductive_attempts == unproductive_attempts
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.USAGE_LIMITED
    assert session.reap_reason is ReapReason.USAGE_LIMIT_MID_TURN


def test_reconcile_backstop_skips_row_whose_act_closed_the_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final transition fails after the session was closed (#2324).

    Later in the same tick the COMPLETED-session backstop finds a closed
    session over a RUNNING row -- the shape it reverts, charging an attempt.
    The row carries the act's intent, so the backstop leaves it, and the next
    tick's resume finishes it uncharged.
    """
    _seed_mid_turn_limit(
        tmp_path, monkeypatch, config=_auto_config(), with_phantom=False
    )
    before = load_dev_queue().tasks[0].unproductive_attempts
    _fail_mid_turn_dev_queue_save(monkeypatch, nth=3)

    assert _MID_TURN_TICKET not in reconcile().reverted_ticket_ids
    assert load_state().sessions[0].status is SessionStatus.COMPLETED
    _assert_act_still_in_flight(unproductive_attempts=before)

    _assert_act_finished_uncharged(reconcile(), unproductive_attempts=before)


def test_reconcile_phantom_sweep_skips_session_whose_act_stopped_it(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session the act stopped but has not closed is not a crash (#2324).

    An earlier tick's act stopped the surface and died before the close, so
    the session is ACTIVE and off the roster: a phantom by shape. This tick's
    resume fails at its first step, and the phantom sweep that runs after it
    would crash-complete the session and revert the row with a charge. It
    skips the row carrying the intent instead; the next tick finishes it.
    """
    _seed_mid_turn_limit(
        tmp_path, monkeypatch, config=_auto_config(), with_phantom=False
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "someone-else-0000-4000-8000-000000000000"}],
    )
    now = datetime.now(UTC)
    store = load_dev_queue()
    store.tasks[0].usage_limit_act = UsageLimitAct(
        session_id=_MID_TURN_TICKET,
        branch="auto",
        started_at=now,
        reset_at=None,
        until=now + timedelta(minutes=30),
        audited_at=now,
    )
    save_dev_queue(store)
    before = store.tasks[0].unproductive_attempts
    calls: list[int] = []

    def _lockout_audit_failing_once(
        etype: OrchestratorEventType,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> None:
        calls.append(1)
        if len(calls) == 1:
            msg = "inbox write failed"
            raise OSError(msg)
        record_event(etype, payload, correlation_id=correlation_id)

    monkeypatch.setattr("cw.dispatch_state.record_event", _lockout_audit_failing_once)

    report = reconcile()

    assert _MID_TURN_TICKET not in report.reverted_ticket_ids
    assert _MID_TURN_TICKET not in report.phantom_session_ids
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.completed_reason is None
    _assert_act_still_in_flight(unproductive_attempts=before)
    assert read_events(event_types=[OrchestratorEventType.SESSION_REAP_PROPOSED]) == []

    _assert_act_finished_uncharged(reconcile(), unproductive_attempts=before)


# ---------------------------------------------------------------------------
# #2524 -- stranded routed-result sweep, wired into _reconcile_locked
# ---------------------------------------------------------------------------

_ROUTED_REF = "ab12cd34"
_ROUTED_SID = "2524"


def _seed_routed_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: TicketTask | None = None,
) -> None:
    """Persist an ACTIVE routed session (31m stale) + its advanced row.

    Also writes a ``client-a`` clients.yaml entry: reconcile hands the sweep
    its configured-client rollout gate, so an unconfigured client is never
    paged (``tmp_path`` is the redirected config dir ``tmp_config_dir``).
    """
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    home = Path.home()
    worktree = tmp_path / "wt-routed"
    _stamp_transcript_age(home, worktree, stale_minutes=31, surface_ref=_ROUTED_REF)
    sess = _mk_routed_session(_ROUTED_SID, worktree, surface_ref=_ROUTED_REF)
    save_state(CwState(sessions=[sess]))
    if row is None:
        row = TicketTask(
            ticket_id=_ROUTED_SID,
            client="client-a",
            status=QueueItemStatus.PENDING,
            stage=Stage.REVIEW,
        )
    save_dev_queue(DevQueueStore(tasks=[row]))


def _routed_pages() -> list[dict[str, object]]:
    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    return [
        dict(e.payload)
        for e in events
        if e.payload.get("paused_status") == "routed_result_session_stranded"
    ]


def _roster_has_routed_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": f"{_ROUTED_REF}-0000-4000-8000-000000000000"}],
    )


def test_reconcile_pages_stranded_routed_session_once_across_two_ticks(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_routed_session(tmp_path, monkeypatch)
    _roster_has_routed_ref(monkeypatch)

    reconcile()
    reconcile()

    pages = _routed_pages()
    assert len(pages) == 1
    assert pages[0]["session_id"] == _ROUTED_SID
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.reap_proposed_at is not None
    proposals = read_events(event_types=[OrchestratorEventType.SESSION_REAP_PROPOSED])
    assert [p.payload["proposed_action"] for p in proposals] == [
        "close_routed_result_session"
    ]


def test_reconcile_auto_policy_still_only_pages_never_closes(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_routed_session(tmp_path, monkeypatch)
    _roster_has_routed_ref(monkeypatch)
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)

    reconcile()

    assert len(_routed_pages()) == 1
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.completed_reason is None
    assert daemon.stop_calls == []


def test_reconcile_skips_sweep_on_daemon_outage_guard_return(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_routed_session(tmp_path, monkeypatch)
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

    reconcile()

    assert _routed_pages() == []
    assert load_state().sessions[0].reap_proposed_at is None


def test_reconcile_skips_sweep_for_unconfigured_client(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_routed_session(tmp_path, monkeypatch)
    _roster_has_routed_ref(monkeypatch)
    (tmp_path / ".config" / "cw" / "clients.yaml").unlink()

    reconcile()

    assert _routed_pages() == []
    assert load_state().sessions[0].reap_proposed_at is None


def test_reconcile_leaves_running_row_session_alone(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_routed_session(
        tmp_path,
        monkeypatch,
        row=TicketTask(
            ticket_id=_ROUTED_SID,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            stage=Stage.REVIEW,
            session_id=_ROUTED_SID,
        ),
    )
    _roster_has_routed_ref(monkeypatch)

    reconcile()

    assert _routed_pages() == []
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.reap_proposed_at is None


# ---------------------------------------------------------------------------
# #1232: every reconcile daemon stop (bar the mid-turn usage-limit act) runs
# after sessions_lock releases, on a session already terminal on disk.
# ---------------------------------------------------------------------------

_STOP_SITE_STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_DECOY_AGENT = {"sessionId": "decoy000-0000-0000-0000-000000000000"}


def _routed_candidate(sid: str) -> ReapCandidate:
    """A ROUTE_EMITTED_SENTINEL candidate with no ticket, so the route is
    accepted without dev-queue arbitration."""
    return ReapCandidate(
        session_id=sid,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=None,
        routed_sentinel=AutoDevResult.model_validate(_shipped_salvage_payload()),
    )


def _stub_detect(monkeypatch: pytest.MonkeyPatch, name: str, sid: str) -> None:
    monkeypatch.setattr(
        f"cw.reconcile.core.{name}",
        lambda *_a, **_k: [_routed_candidate(sid)],
    )


def _seed_phantom_merged(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path, _daemon: LockProbeDaemon
) -> str:
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", lambda _tid, **_kw: (True, True)
    )
    sess = _mk_phantom_daemon_session(
        "ph-merged", _STOP_SITE_STARTED_AT, surface_ref="deadref1"
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="ph-merged",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="ph-merged",
                )
            ]
        )
    )
    return sess.id


def _seed_phantom_routed(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path, _daemon: LockProbeDaemon
) -> str:
    sess = _mk_phantom_daemon_session(
        "ph-routed", _STOP_SITE_STARTED_AT, surface_ref="deadref2"
    )
    sess.name = "client-a/adhoc-ph-routed"
    save_state(CwState(sessions=[sess]))
    _stub_detect(monkeypatch, "_detect_phantom_candidates", sess.id)
    return sess.id


def _seed_idle_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, daemon: LockProbeDaemon
) -> str:
    short_id = daemon.seed_live_worker(tmp_path / "wt-idle")
    sess = _make_daemon_session(
        id="idle-routed",
        name="client-a/adhoc-idle-routed",
        surface_ref=short_id,
        started_at=_STOP_SITE_STARTED_AT,
    )
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [_DECOY_AGENT, {"sessionId": short_id}],
    )
    _stub_detect(monkeypatch, "_detect_idle_candidates", sess.id)
    return sess.id


def _seed_stalled_foreign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _daemon: LockProbeDaemon
) -> str:
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", lambda _tid, **_kw: (False, True)
    )
    sess = _mk_headless_daemon_session(
        "st-foreign",
        tmp_path / "wt-stalled",
        _STOP_SITE_STARTED_AT,
        surface_ref="stref001",
    )
    sess.last_result = _shipped_salvage_payload()
    save_state(CwState(sessions=[sess]))
    return sess.id


def _seed_stalled_routed(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path, _daemon: LockProbeDaemon
) -> str:
    sess = _make_daemon_session(
        id="st-routed",
        name="client-a/adhoc-st-routed",
        surface_ref="stref002",
        started_at=_STOP_SITE_STARTED_AT,
    )
    sess.last_result = _stage_complete_payload()
    save_state(CwState(sessions=[sess]))
    _stub_detect(monkeypatch, "_detect_stalled_candidates", sess.id)
    return sess.id


def _seed_leaked_worker(
    _monkeypatch: pytest.MonkeyPatch, tmp_path: Path, daemon: LockProbeDaemon
) -> str:
    short_id = daemon.seed_live_worker(tmp_path / "wt-leaked")
    sess = _make_daemon_session(
        id="leaked01",
        name="client-a/auto-dev/GEN-77",
        status=SessionStatus.COMPLETED,
        surface_ref=short_id,
    )
    save_state(CwState(sessions=[sess]))
    return sess.id


_STOP_SITE_SEEDERS = {
    "phantom_merged_auto": _seed_phantom_merged,
    "phantom_routed": _seed_phantom_routed,
    "idle_routed": _seed_idle_routed,
    "stalled_foreign": _seed_stalled_foreign,
    "stalled_routed": _seed_stalled_routed,
    "leaked_worker": _seed_leaked_worker,
}


def _install_probe_daemon(monkeypatch: pytest.MonkeyPatch) -> LockProbeDaemon:
    daemon = LockProbeDaemon()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", lambda: [_DECOY_AGENT])
    return daemon


@pytest.mark.parametrize("site", list(_STOP_SITE_SEEDERS), ids=list(_STOP_SITE_SEEDERS))
def test_reconcile_stop_runs_with_lock_free_on_a_terminal_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    """Each reconcile stop site's surface stop happens after sessions_lock
    releases, and the session is already COMPLETED on disk when it does."""
    daemon = _install_probe_daemon(monkeypatch)
    sid = _STOP_SITE_SEEDERS[site](monkeypatch, tmp_path, daemon)

    reconcile()

    assert daemon.probes == [("free", {sid: SessionStatus.COMPLETED})]
    assert len(daemon.stop_calls) == 1


class _FirstStopRaisesDaemon(LockProbeDaemon):
    def stop(self, short_id: str) -> None:
        super().stop(short_id)
        if len(self.stop_calls) == 1:
            msg = "daemon socket gone"
            raise OSError(msg)


def _seed_two_leaked_workers(tmp_path: Path, daemon: FakeNativeDaemonClient) -> None:
    sessions = [
        _make_daemon_session(
            id=f"leaked0{n}",
            status=SessionStatus.COMPLETED,
            surface_ref=daemon.seed_live_worker(tmp_path / f"wt-{n}"),
        )
        for n in (1, 2)
    ]
    save_state(CwState(sessions=sessions))


def test_a_raising_stop_neither_skips_the_next_stop_nor_fails_reconcile(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = _FirstStopRaisesDaemon()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", lambda: [_DECOY_AGENT])
    _seed_two_leaked_workers(tmp_path, daemon)

    with caplog.at_level("ERROR", logger="cw.reconcile.deferred"):
        report = reconcile()

    assert isinstance(report, ReconcileReport)
    assert len(daemon.stop_calls) == 2
    assert [lock for lock, _ in daemon.probes] == ["free", "free"]
    assert "post_lock_job_failed label=leaked_worker_stop:" in caplog.text


def test_queued_stops_still_drain_when_a_later_in_lock_step_raises(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop queued early in the locked body still runs from reconcile()'s
    ``finally`` when a later in-lock step raises, and the original exception
    still propagates."""
    daemon = _install_probe_daemon(monkeypatch)
    sid = _seed_leaked_worker(monkeypatch, tmp_path, daemon)

    def _boom(**_kwargs: object) -> None:
        msg = "later in-lock step exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.reconcile.core.run_escalation_sweep", _boom)

    with pytest.raises(RuntimeError, match="later in-lock step exploded"):
        reconcile()

    assert daemon.probes == [("free", {sid: SessionStatus.COMPLETED})]


def test_two_consecutive_reconciles_issue_exactly_one_stop(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install_probe_daemon(monkeypatch)
    _seed_leaked_worker(monkeypatch, tmp_path, daemon)

    reconcile()
    reconcile()

    assert len(daemon.stop_calls) == 1


def test_stalled_completion_with_a_live_worker_is_stopped_once_not_as_leaked(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stalled sweep completes the session and queues its stop before the
    leaked-worker sweep runs in the same tick; the leaked sweep sees the now
    terminal session's live worker and must skip it -- one stop, and no
    spurious DAEMON_LEAKED_WORKER_STOPPED audit."""
    daemon = _install_probe_daemon(monkeypatch)
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", lambda _tid, **_kw: (False, True)
    )
    worktree = tmp_path / "wt-stalled-live"
    short_id = daemon.seed_live_worker(worktree)
    sess = _mk_headless_daemon_session(
        "st-live", worktree, _STOP_SITE_STARTED_AT, surface_ref=short_id
    )
    sess.last_result = _shipped_salvage_payload()
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [_DECOY_AGENT, {"sessionId": short_id}],
    )

    reconcile()
    reconcile()

    assert daemon.probes == [("free", {"st-live": SessionStatus.COMPLETED})]
    assert daemon.stop_calls == [short_id]
    leaked = read_events(
        consumer="test-1232-stalled-live-worker",
        event_types=[OrchestratorEventType.DAEMON_LEAKED_WORKER_STOPPED],
    )
    assert leaked == []


def test_review_dispatch_still_runs_when_the_stop_drain_is_interrupted(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review dispatch sits in a nested ``finally``: a KeyboardInterrupt
    out of the stop drain still lets the already-latched review jobs run, and
    the interrupt propagates."""
    order: list[str] = []

    def _interrupted_drain(_jobs: DeferredReconcileJobs) -> None:
        order.append("post_lock_drain")
        raise KeyboardInterrupt

    monkeypatch.setattr("cw.reconcile.core.run_post_lock_jobs", _interrupted_drain)
    monkeypatch.setattr(
        "cw.reconcile.core.dispatch_deferred_review_jobs",
        lambda _sink: order.append("review_dispatch"),
    )

    with pytest.raises(KeyboardInterrupt):
        reconcile(dispatch_review_jobs=True)

    assert order == ["post_lock_drain", "review_dispatch"]


def test_reconcile_without_review_flag_still_drains_post_lock_jobs_once(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drained: list[DeferredReconcileJobs] = []
    monkeypatch.setattr("cw.reconcile.core.run_post_lock_jobs", drained.append)

    reconcile()

    assert len(drained) == 1
    assert drained[0].review is None


# --- #1232 commit B: the recipes' gh calls run after sessions_lock releases --


def _seed_gate_and_reviewer_rows(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GEN-1: a clean review gate the auto-approve recipe releases (one audit
    comment). GEN-2: a no_reviewer PR the request_reviewer recipe acts on."""
    write_clients_yaml(
        ClientSpec("acme", tmp_config_dir, default_branch="main", lanes=_GATE_LANES)
    )
    gate_session = _gate_session(last_result=_clean_result())
    gate_session.status = SessionStatus.COMPLETED
    save_state(CwState(sessions=[gate_session]))
    reviewer_row = _gate_task(
        ticket_id="GEN-2",
        session_id=None,
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state=_pr_state(attention_state="no_reviewer"),
        review_recipes={RECIPE_REQUEST_REVIEWER: True},
    )
    save_dev_queue(DevQueueStore(tasks=[_gate_task(ticket_id="GEN-1"), reviewer_row]))
    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
        lambda: OrchestratorConfig(review_recipes_enabled=True),
    )
    monkeypatch.setattr(
        "cw.reconcile.review_recipes.request_reviewer.resolve_review_strategy",
        lambda _root: ReviewStrategy("repo_owner", "alice"),
    )


def _record_gh_lock_state(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[str, ...], bool]]:
    """Patch the gh subprocess seam to record (argv head, lock free?) per call."""
    calls: list[tuple[tuple[str, ...], bool]] = []

    def _fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((tuple(argv[:3]), probe_sessions_lock_free()))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("cw.gh._sp.run", _fake_run)
    return calls


@pytest.mark.parametrize(
    "dispatch_review_jobs", [True, False], ids=["dispatch_loop", "read_only"]
)
def test_reconcile_recipe_gh_calls_run_with_sessions_lock_free(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_review_jobs: bool,
) -> None:
    """A real reconcile(): the gate recipe's audit comment and the reviewer
    request both reach gh only after sessions_lock releases, in the order the
    recipes queued them. The reviewer request fires from a read-only call too
    (no dispatch_review_jobs): its one-shot latch is stamped in-lock either
    way, so the sink it lands on is always built and drained."""
    _seed_gate_and_reviewer_rows(tmp_config_dir, monkeypatch)
    gh_calls = _record_gh_lock_state(monkeypatch)

    reconcile(dispatch_review_jobs=dispatch_review_jobs)

    assert gh_calls == [
        (("gh", "issue", "comment"), True),
        (("gh", "pr", "edit"), True),
    ]
    rows = {t.ticket_id: t for t in load_dev_queue().tasks}
    assert rows["GEN-1"].stage == Stage.FINALIZE
    assert rows["GEN-2"].request_reviewer_fired_at is not None
    assert read_events(event_types=[OrchestratorEventType.PR_ACTION_FAILED]) == []


class TestUnownedRunningSweepWiring:
    """#2591: the unbound-row adoption sweep runs in both terminal tails,
    before the TIMED_OUT backstop, and not on the daemon-outage early return."""

    @staticmethod
    def _record_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        order: list[str] = []
        real_adopt = reconcile_core.run_unowned_running_recovery
        real_revert = reconcile_core.revert_timed_out_tasks

        def _adopt(*, clients: dict[str, ClientConfig]) -> list[str]:
            order.append("unowned_running")
            return real_adopt(clients=clients)

        def _revert(dirty_checks: DirtyChecks | None = None) -> list[str]:
            order.append("revert_timed_out")
            return real_revert(dirty_checks)

        monkeypatch.setattr(reconcile_core, "run_unowned_running_recovery", _adopt)
        monkeypatch.setattr(reconcile_core, "revert_timed_out_tasks", _revert)
        return order

    def test_runs_before_the_timed_out_backstop_without_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._record_order(monkeypatch)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        report = reconcile()

        assert report.phantom_session_ids == []
        assert order == ["unowned_running", "revert_timed_out"]

    def test_runs_before_the_timed_out_backstop_with_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
        save_state(CwState(sessions=[_mk_session("s1", "missing-ref")]))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        order = self._record_order(monkeypatch)

        report = reconcile()

        assert report.phantom_session_ids == ["s1"]
        assert order == ["unowned_running", "revert_timed_out"]

    def test_skipped_on_the_daemon_outage_early_return(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[_mk_session("s1", "live-ref")]))
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        order = self._record_order(monkeypatch)

        reconcile()

        assert order == []

    def test_phantom_sweep_reverts_an_unbound_phantom_row_first(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A row whose claim session is a phantom is the phantom sweep's (keyed
        by ticket id); adoption then finds no RUNNING row and does nothing."""
        from cw.worktree import worktree_path_for
        from tests.conftest import _write_hook_context_file

        monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
        write_clients_yaml(client)
        wt = worktree_path_for(client, f"{client.feature_branch_prefix}/TKT-1")
        wt.mkdir(parents=True)
        _write_hook_context_file(
            wt,
            session_id="sess-daemon",
            ticket_id="TKT-1",
            client="client-a",
            task=_make_ticket_task(ticket_id="TKT-1", client="client-a", attempts=1),
        )
        sess = _mk_session("sess-daemon", "dead-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/TKT-1"
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="TKT-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        attempts=1,
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        adopted = capture_events(
            "cw.reconcile.unowned_running",
            OrchestratorEventType.TASK_SESSION_ADOPTED,
        )

        report = reconcile()

        assert "TKT-1" in report.reverted_ticket_ids
        row = load_dev_queue().tasks[0]
        assert row.status == QueueItemStatus.PENDING
        assert row.session_id is None
        assert adopted == []


class TestDirtyChecksPrePass:
    """#2548: the worktree dirty checks are captured in a lockless pre-pass and
    the in-lock phantom detect and terminal backstops only look them up."""

    @staticmethod
    def _record_consumers(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        seen: dict[str, object] = {}
        real_detect = reconcile_core._detect_phantom_candidates

        def _detect(*args: Any, **kwargs: Any) -> list[ReapCandidate]:
            seen["detect"] = kwargs["dirty_checks"]
            return real_detect(*args, **kwargs)

        def _recorder(name: str) -> Callable[[DirtyChecks | None], list[str]]:
            def _revert(dirty_checks: DirtyChecks | None = None) -> list[str]:
                seen[name] = dirty_checks
                return []

            return _revert

        monkeypatch.setattr(reconcile_core, "_detect_phantom_candidates", _detect)
        monkeypatch.setattr(
            reconcile_core, "revert_timed_out_tasks", _recorder("timed_out")
        )
        monkeypatch.setattr(
            reconcile_core, "revert_completed_silent_tasks", _recorder("completed")
        )
        return seen

    @pytest.mark.parametrize("with_phantom", [True, False])
    def test_captured_unlocked_and_threaded_by_identity(
        self, monkeypatch: pytest.MonkeyPatch, with_phantom: bool
    ) -> None:
        sessions = [_mk_session("s1", "missing-ref")] if with_phantom else []
        save_state(CwState(sessions=sessions))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        sentinel = DirtyChecks()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(*, config: OrchestratorConfig) -> DirtyChecks:
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(reconcile_core, "_capture_dirty_checks", _fake_capture)
        seen = self._record_consumers(monkeypatch)

        reconcile()

        assert lock_free_at_capture == [True]
        assert seen.pop("timed_out") is sentinel
        assert seen.pop("completed") is sentinel
        if with_phantom:
            assert seen.pop("detect") is sentinel
        assert seen == {}

    def test_store_is_built_with_the_per_tick_bounds(self) -> None:
        save_state(CwState(sessions=[]))

        checks = reconcile_core._capture_dirty_checks(config=OrchestratorConfig())

        assert checks.budget_seconds == DIRTY_CHECK_BUDGET_SECONDS
        assert checks.max_captures == DIRTY_CHECK_MAX_PER_TICK
        assert checks.captures == 0

    def test_idle_tick_makes_no_roster_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No live DAEMON session with a worktree: no extra `claude agents`."""
        save_state(CwState(sessions=[_mk_session("s1", "live-ref")]))
        calls: list[int] = []

        def _roster() -> list[dict[str, object]]:
            calls.append(1)
            return []

        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _roster)

        reconcile_core._capture_dirty_checks(config=OrchestratorConfig())

        assert calls == []

    def test_unreadable_state_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = reconcile_core.load_dev_queue
        loads: list[int] = []

        def _dirty_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _DIRTY_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(reconcile_core, "load_dev_queue", _dirty_load_fails)
        seen = self._record_consumers(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=reconcile_core.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        checks = seen["timed_out"]
        assert isinstance(checks, DirtyChecks)
        assert checks.budget_seconds is None
        assert checks.max_captures is None
        assert "dirty-check pre-pass could not read state" in caplog.text

    def test_capture_does_not_swallow_unexpected_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only state/dev-queue read errors are contained: a reader bug
        propagates out of the pre-pass, as it did from the old in-lock check."""
        session = _make_daemon_session(
            id="to-1",
            name="client-a/auto-dev/GEN-1",
            status=SessionStatus.TIMED_OUT,
            worktree_path=tmp_path / "wt",
            surface_ref=None,
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="to-1",
                    )
                ]
            )
        )

        def _boom(_client: str, _path: object) -> str | None:
            msg = "a dirty-check bug"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._shared.worktree_dirty_reason_by_path", _boom)

        with pytest.raises(RuntimeError, match="a dirty-check bug"):
            reconcile_core._capture_dirty_checks(config=OrchestratorConfig())

    def test_reconcile_locked_without_dirty_checks_passes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        seen = self._record_consumers(monkeypatch)

        with sessions_lock():
            reconcile_core._reconcile_locked(deferred=DeferredReconcileJobs())

        assert seen == {"timed_out": None, "completed": None}

    def test_roster_race_session_defers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M8: live in the pre-pass roster, absent from the in-lock one, so it
        is a phantom with no capture: it defers a tick and is never parked."""
        surface_ref = "racesess"
        session = _make_daemon_session(
            id="race-1",
            name="client-a/auto-dev/GEN-8",
            surface_ref=surface_ref,
            worktree_path=tmp_path / "wt-race",
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-8",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="race-1",
                    )
                ]
            )
        )
        rosters = iter(
            [[{"sessionId": f"{surface_ref}-full-uuid"}], [{"sessionId": "decoy000"}]]
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json", lambda: next(rosters)
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        def _no_live_check(_client: str, _path: object) -> str | None:
            msg = "the roster-race session must not be dirty-checked"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_dirty_reason_by_path", _no_live_check
        )

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            report = reconcile()

        assert report.phantom_session_ids == ["race-1"]
        row = load_dev_queue().tasks[0]
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "race-1"
        assert load_state().sessions[0].status is SessionStatus.ACTIVE
        assert _attention_events("m8", "GEN-8") == []
        push = reconcile_core._deps.fire_push_notification
        assert isinstance(push, MagicMock)
        push.assert_not_called()
        assert len(unavailable_records(caplog, "race-1")) == 1
