"""Unit tests for cw.reconcile.stalled — liveness-veto and wall-clock backstop.

Wall-clock-budget liveness veto, stage_complete backstop harvest, race
guards, and the per-policy act dispatchers (signal-only / per-lane) plus the
salvage-skip attention latch. Split out of test_reconcile_stalled.py per R5.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.config import (
    load_state,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    DEFAULT_LANE,
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
    _SALVAGE_SKIP_REASON,
    HEADLESS_TIMEOUT_SECONDS,
    revert_stalled_headless_sessions,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
)
from tests.conftest import (
    CapturedEvent,
    _write_idle_transcript,
)


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


def test_stalled_veto_suppresses_park_when_nested_subagent_transcript_fresh(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1431: same shape as test_stalled_veto_park_proceeds_once_quiet (stale
    flat registered transcript past the per-stage floor), but with a FRESH
    nested subagent transcript (``<uuid>/subagents/agent-*.jsonl``) sibling.
    Must flip the outcome from REVERT_TASK to PARK_VETOED -- pre-fix, the
    non-recursive glob is blind to the nested file and this still parks."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-nested-fresh"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("veto-nested-fresh-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="veto-nested-fresh-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-nested-fresh-1",
        stage=Stage.PLAN,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    transcript = _write_idle_transcript(home, worktree)
    # 40 minutes stale — past the 15-min PLAN-stage floor -> not LIVE on its own.
    stale_ts = (now - timedelta(minutes=40)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))

    # Fresh nested subagent transcript sibling — 1 minute stale, well under
    # the floor -- invisible to a non-recursive glob.
    nested = _write_idle_transcript(
        home, worktree, filename="some-uuid/subagents/agent-abc.jsonl"
    )
    nested_ts = (now - timedelta(minutes=1)).timestamp()
    os.utime(str(nested), (nested_ts, nested_ts))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=_auto_config(),
        task_by_ticket={"veto-nested-fresh-1": task},
    )

    assert any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)
    assert not any(c.proposed_action == ProposedAction.REVERT_TASK for c in candidates)


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

        auto, escalate = _route_stalled_by_policy(
            [reset_candidate],
            config=OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
            merged_ticket_ids=frozenset(),
            gh_blocked_ticket_ids=frozenset(),
        )

        assert auto == [reset_candidate]
        assert escalate == []

    def test_main_drift_module_docstring_no_stale_929_citation(self) -> None:
        """main_drift.py's docstring must not attribute the counter to #929 (#974)."""
        import cw.reconcile.main_drift as main_drift_mod

        assert main_drift_mod.__doc__ is not None
        assert "#929" not in main_drift_mod.__doc__
        assert "consecutive_freshness_blocks" in main_drift_mod.__doc__


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


# ---------------------------------------------------------------------------
# GitHub #1445 — bounded liveness veto: after park_veto_cap consecutive
# post-budget vetoes the veto stops suppressing the park, and cap-fire
# emits an immediate session.needs_attention at BOTH call sites (parity).
# ---------------------------------------------------------------------------


def _write_fresh_transcript(home: Path, worktree: Path, now: datetime) -> None:
    """Write a transcript for *worktree* stamped 5 min stale (LIVE at PLAN)."""
    transcript = _write_idle_transcript(home, worktree)
    fresh_ts = (now - timedelta(minutes=5)).timestamp()
    os.utime(str(transcript), (fresh_ts, fresh_ts))


def test_liveness_veto_candidate_stamps_incrementing_veto_count(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LIVE session below the cap yields a PARK_VETOED candidate whose
    new_veto_count is the current count + 1, and cap_exhausted is False (#1445)."""
    from cw.reconcile import ProposedAction
    from cw.reconcile.stalled import _liveness_veto_candidate

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-count"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("veto-count-1", worktree, started_at)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id="veto-count-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-count-1",
        stage=Stage.PLAN,
    )
    # cap=2: counts 0 and 1 are both below cap -> both veto.
    config = OrchestratorConfig(park_veto_cap=2)

    sess.consecutive_park_vetoes = 0
    cand0, exhausted0 = _liveness_veto_candidate(
        sess,
        task,
        "veto-count-1",
        3700.0,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )
    assert cand0 is not None
    assert cand0.proposed_action == ProposedAction.PARK_VETOED
    assert cand0.new_veto_count == 1
    assert exhausted0 is False

    sess.consecutive_park_vetoes = 1
    cand1, exhausted1 = _liveness_veto_candidate(
        sess,
        task,
        "veto-count-1",
        3700.0,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )
    assert cand1 is not None
    assert cand1.new_veto_count == 2
    assert exhausted1 is False


def test_liveness_veto_candidate_returns_none_once_cap_reached(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LIVE session already at the cap returns (None, True): the veto is
    exhausted and the caller must fall through to its park/revert (#1445)."""
    from cw.reconcile.stalled import _liveness_veto_candidate

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-capped"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("veto-capped-1", worktree, started_at)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id="veto-capped-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-capped-1",
        stage=Stage.PLAN,
    )
    config = OrchestratorConfig(park_veto_cap=2)
    sess.consecutive_park_vetoes = 2

    cand, exhausted = _liveness_veto_candidate(
        sess,
        task,
        "veto-capped-1",
        3700.0,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )
    assert cand is None
    assert exhausted is True


def test_liveness_veto_candidate_never_live_returns_false_not_exhausted(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely stale session returns (None, False): "not live" must never
    be misreported as "cap fired", even with a counter sitting at the cap
    (#1445 — the two-source-of-truth requirement)."""
    from cw.reconcile.stalled import _liveness_veto_candidate

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-stale"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("veto-stale-1", worktree, started_at)
    transcript = _write_idle_transcript(home, worktree)
    # 40 minutes stale — past the 15-min PLAN floor -> not LIVE.
    stale_ts = (now - timedelta(minutes=40)).timestamp()
    os.utime(str(transcript), (stale_ts, stale_ts))
    task = TicketTask(
        ticket_id="veto-stale-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-stale-1",
        stage=Stage.PLAN,
    )
    config = OrchestratorConfig(park_veto_cap=2)
    sess.consecutive_park_vetoes = 2  # at cap, but transcript is genuinely stale

    cand, exhausted = _liveness_veto_candidate(
        sess,
        task,
        "veto-stale-1",
        3700.0,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )
    assert cand is None
    assert exhausted is False


def test_stalled_veto_bounded_falls_through_to_revert_task(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE session below the retry cap but at the veto cap -> REVERT_TASK with
    veto_cap_exhausted=True, not PARK_VETOED (#1445)."""
    from cw.reconcile import ProposedAction, _detect_stalled_candidates

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-bound-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("bound-revert-1", worktree, started_at)
    sess.consecutive_park_vetoes = 2  # at cap
    state = CwState(sessions=[sess])
    save_state(state)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id="bound-revert-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="bound-revert-1",
        stage=Stage.PLAN,
        attempts=0,  # below retry cap -> wall-clock revert path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=OrchestratorConfig(park_veto_cap=2),
        task_by_ticket={"bound-revert-1": task},
    )
    revert = next(
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    )
    assert revert.veto_cap_exhausted is True
    assert revert.reap_reason == ReapReason.WALL_CLOCK_BUDGET
    assert not any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)


def test_stalled_veto_bounded_falls_through_to_park_blocked_on_user(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE session at BOTH the retry cap and the veto cap -> PARK_BLOCKED_ON_USER
    with veto_cap_exhausted=True, not PARK_VETOED (#1445)."""
    from cw.reconcile import (
        DEFAULT_STALLED_RETRY_CAP,
        ProposedAction,
        _detect_stalled_candidates,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-bound-park"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("bound-park-1", worktree, started_at)
    sess.consecutive_park_vetoes = 2  # at veto cap
    state = CwState(sessions=[sess])
    save_state(state)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id="bound-park-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="bound-park-1",
        stage=Stage.PLAN,
        attempts=DEFAULT_STALLED_RETRY_CAP,  # at retry cap -> cap-park path
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=OrchestratorConfig(park_veto_cap=2),
        task_by_ticket={"bound-park-1": task},
    )
    park = next(
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    )
    assert park.veto_cap_exhausted is True
    assert park.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED
    assert not any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)


def test_act_on_stalled_park_vetoed_persists_consecutive_count(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """A PARK_VETOED candidate's new_veto_count is persisted onto the session
    and carried into the event payload as consecutive_vetoes (#1445)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    worktree = tmp_path / "wt-veto-persist"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("veto-persist-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="veto-persist-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-persist-1",
        stage=Stage.IMPL,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="veto-persist-1",
        proposed_action=ProposedAction.PARK_VETOED,
        ticket_id="veto-persist-1",
        elapsed_seconds=3700.0,
        client="client-a",
        stage=Stage.IMPL,
        stale_minutes=4.2,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        new_veto_count=1,
    )

    _act_on_stalled_candidates(state, [candidate], now=now)

    reloaded = load_state()
    s = next(s for s in reloaded.sessions if s.id == "veto-persist-1")
    assert s.consecutive_park_vetoes == 1

    events = read_events(
        consumer="test-veto-persist-1",
        event_types=[OrchestratorEventType.SESSION_PARK_VETOED],
    )
    assert len(events) == 1
    assert events[0].payload["consecutive_vetoes"] == 1


def test_stalled_veto_cap_end_to_end_via_revert_stalled_headless_sessions(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full N-tick simulation (park_veto_cap=2): tick 1 vetoes (count->1),
    tick 2 vetoes (count->2), tick 3 falls through to the terminal cap-park
    disposition (#1445)."""
    monkeypatch.setattr(
        "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification", lambda *_a, **_kw: None
    )
    monkeypatch.setattr("cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None)

    from cw.reconcile import DEFAULT_STALLED_RETRY_CAP

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-veto-e2e-bound"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("veto-e2e-bound-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id="veto-e2e-bound-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="veto-e2e-bound-1",
        stage=Stage.PLAN,
        attempts=DEFAULT_STALLED_RETRY_CAP,  # cap-park path once veto exhausts
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    config = OrchestratorConfig(park_veto_cap=2)

    # Tick 1 — veto, count -> 1.
    revert_stalled_headless_sessions(state, now=now, config=config)
    assert state.sessions[0].consecutive_park_vetoes == 1
    assert state.sessions[0].status == SessionStatus.ACTIVE

    # Tick 2 — veto, count -> 2.
    revert_stalled_headless_sessions(state, now=now, config=config)
    assert state.sessions[0].consecutive_park_vetoes == 2
    assert state.sessions[0].status == SessionStatus.ACTIVE

    # Tick 3 — veto exhausted, falls through to the cap-park terminal.
    revert_stalled_headless_sessions(state, now=now, config=config)
    assert state.sessions[0].status == SessionStatus.TIMED_OUT
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "veto-e2e-bound-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER


@pytest.mark.parametrize(
    ("attempts", "expected_paused_status"),
    [
        (2, "stalled_retry_cap_parked"),
        (0, "wall_clock_budget"),
    ],
    ids=["stalled_retry_cap_parked", "wall_clock_budget"],
)
def test_veto_cap_exhaustion_emits_immediate_needs_attention(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
    expected_paused_status: str,
) -> None:
    """Parity: when the veto cap is exhausted, a session.needs_attention fires
    this same tick at BOTH cap-fire sites — the retry-cap park (via its
    pre-existing emission) and the wall-clock-budget SIGNAL_ONLY reroute (via
    the new escalation loop) — each with a push notification and no
    daemon-stop / worktree removal on the wall-clock path (#1445)."""
    from cw.reconcile import (
        ProposedAction,
        _act_on_stalled_candidates,
        _detect_stalled_candidates,
    )

    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    push_calls: list[object] = []
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification",
        lambda *a, **kw: push_calls.append((a, kw)),
    )
    removed: list[object] = []
    monkeypatch.setattr(
        "cw.reconcile._shared.remove_worktree",
        lambda *a, **kw: removed.append((a, kw)),
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / f"wt-veto-attn-{expected_paused_status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sid = f"veto-attn-{expected_paused_status}"
    sess = _mk_headless_daemon_session(sid, worktree, started_at)
    sess.consecutive_park_vetoes = 2  # at veto cap
    state = CwState(sessions=[sess])
    save_state(state)
    _write_fresh_transcript(home, worktree, now)
    task = TicketTask(
        ticket_id=sid,
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id=sid,
        stage=Stage.PLAN,
        attempts=attempts,
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    # Default SIGNAL_ONLY policy so the wall-clock REVERT_TASK reroutes to
    # BLOCKED_ON_USER (never a destructive auto-revert).
    config = OrchestratorConfig(park_veto_cap=2)

    candidates = _detect_stalled_candidates(
        state, now=now, config=config, task_by_ticket={sid: task}
    )
    assert not any(c.proposed_action == ProposedAction.PARK_VETOED for c in candidates)
    _act_on_stalled_candidates(state, candidates, now=now, config=config)

    # Task routes to BLOCKED_ON_USER via either the park path or the
    # SIGNAL_ONLY silent mutation.
    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == sid)
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer=f"test-{sid}",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    attn = [e for e in events if e.payload.get("session_id") == sess.id]
    assert len(attn) == 1
    assert attn[0].payload["paused_status"] == expected_paused_status
    assert push_calls  # a push notification fired this tick

    if expected_paused_status == "wall_clock_budget":
        # Non-destructive escalation: SIGNAL_ONLY never stops the daemon or
        # removes the worktree on this path.
        assert daemon.stop_calls == []
        assert removed == []


def test_stalled_veto_falls_through_ordinary_first_park_no_escalation(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: an ordinary first-ever REVERT_TASK (never vetoed,
    veto_cap_exhausted=False) under SIGNAL_ONLY must route to BLOCKED_ON_USER
    WITHOUT gaining a new session.needs_attention emission (#1445)."""
    from cw.reconcile import ProposedAction, ReapCandidate, _act_on_stalled_candidates

    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    push_calls: list[object] = []
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification",
        lambda *a, **kw: push_calls.append((a, kw)),
    )

    worktree = tmp_path / "wt-ordinary-revert"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
    sess = _mk_headless_daemon_session("ordinary-revert-1", worktree, started_at)
    state = CwState(sessions=[sess])
    save_state(state)
    task = TicketTask(
        ticket_id="ordinary-revert-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="ordinary-revert-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    candidate = ReapCandidate(
        session_id="ordinary-revert-1",
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id="ordinary-revert-1",
        elapsed_seconds=3700.0,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        veto_cap_exhausted=False,
    )

    _act_on_stalled_candidates(state, [candidate], now=now, config=OrchestratorConfig())

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "ordinary-revert-1")
    assert t.status == QueueItemStatus.BLOCKED_ON_USER

    events = read_events(
        consumer="test-ordinary-revert-1",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert events == []
    assert push_calls == []
