"""Unit tests for cw.reconcile.local.

Local dead-process git-harvest: completes + advances a worker whose PID is
dead, PID/start-time recycle detection, no-commits aider_no_output synthesis,
and park_terminal_sibling_tasks policy branches.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import freezegun
import pytest

from cw.auto_dev_result import AutoDevResult
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
    LastResultSource,
    LocalLivenessHandle,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    OPENCODE_NO_OUTPUT,
)
from cw.opencode_runner import (
    make_blocked as make_opencode_blocked,
)
from cw.reconcile import (
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
    reconcile,
)
from tests._reconcile_helpers import (
    _write_staged_clients_yaml,
)
from tests.conftest import _make_daemon_session


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
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
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


def test_local_harvest_stamps_git_synthesis_source(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """RFC 0012 A3 (#1459): a successful git-synthesis harvest routes through
    the door and stamps ``last_result_source == GIT_SYNTHESIS``."""
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-gitsrc", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=5)
    sess = _mk_local_session("harv-gitsrc", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-gitsrc",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-gitsrc",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(state, task_by_ticket)

    harvested = _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    assert harvested == ["harv-gitsrc"]
    reloaded = next(s for s in load_state().sessions if s.id == "harv-gitsrc")
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result_source == LastResultSource.GIT_SYNTHESIS


def test_local_harvest_refused_by_door_leaves_session_and_task_untouched(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """RFC 0012 A3 (#1459): when the door refuses (a foreign terminal result is
    already recorded), the session-completion write is suppressed and no
    SESSION_COMPLETED event fires for the candidate. The task was already routed
    by _apply_sentinel_to_task before the door check (Adopted Assumption 2)."""
    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-refused", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=9)
    sess = _mk_local_session("harv-refused", worktree, liveness)
    foreign = {"status": "shipped", "foreign_authority": True}
    sess.last_result = foreign
    sess.last_result_source = LastResultSource.STOP_HOOK_HARVEST
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-refused",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-refused",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(state, task_by_ticket)

    harvested = _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    # Candidate dropped: not counted as harvested.
    assert harvested == []
    reloaded = next(s for s in load_state().sessions if s.id == "harv-refused")
    # Session left untouched: still ACTIVE, foreign result + source intact.
    assert reloaded.status == SessionStatus.ACTIVE
    assert reloaded.last_result == foreign
    assert reloaded.last_result_source == LastResultSource.STOP_HOOK_HARVEST
    # No SESSION_COMPLETED event fired for the refused candidate.
    events = read_events(
        consumer="test-harvest-refused",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == "harv-refused" for e in events)


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
    from cw.models import LaneConfig
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
# Opencode harvest (#1669) — .cw/opencode.log with sentinel
# ---------------------------------------------------------------------------


def test_local_harvest_opencode_sentinel_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Dead opencode process with sentinel in log → completed with parsed result."""
    worktree = make_git_repo("wt-opencode-harvest-sentinel")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    blocked = make_opencode_blocked(
        ticket_id="T-OC-1", worktree=worktree, reason="test-sentinel"
    )
    sentinel_json = blocked.model_dump_json()
    sentinel_text = f"<<<AUTO_DEV_RESULT\n{sentinel_json}\nAUTO_DEV_RESULT>>>"
    text_event = json.dumps({"type": "text", "part": {"text": sentinel_text}})
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text_event, encoding="utf-8")

    dead_handle = LocalLivenessHandle(pid=999999, start_time_ns=1)
    sess = _mk_local_session("ses-oc-sentinel", worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-OC-1",
                    client="client-a",
                    stage=Stage.IMPL,
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
    )

    candidates = _detect_local_harvest_candidates(load_state())
    assert len(candidates) == 1

    with freezegun.freeze_time("2026-01-01 12:00:00"):
        _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

    state = load_state()
    session = next(s for s in state.sessions if s.id == "ses-oc-sentinel")
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result is not None
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == "test-sentinel"


def test_local_harvest_opencode_no_output(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Dead opencode process with no sentinel in log → OPENCODE_NO_OUTPUT."""
    worktree = make_git_repo("wt-opencode-harvest-no-output")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")

    log_content = json.dumps({"type": "text", "part": {"text": "no sentinel here"}})
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_content, encoding="utf-8")

    dead_handle = LocalLivenessHandle(pid=999999, start_time_ns=1)
    sess = _mk_local_session("ses-oc-no-output", worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-OC-2",
                    client="client-a",
                    stage=Stage.IMPL,
                    status=QueueItemStatus.RUNNING,
                )
            ]
        )
    )

    candidates = _detect_local_harvest_candidates(load_state())
    assert len(candidates) == 1

    with freezegun.freeze_time("2026-01-01 12:00:00"):
        _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )

    state = load_state()
    session = next(s for s in state.sessions if s.id == "ses-oc-no-output")
    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT
