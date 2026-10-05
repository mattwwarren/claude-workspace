"""Unit tests for cw.reconcile.local.

Local dead-process git-harvest: completes + advances a worker whose PID is
dead, PID/start-time recycle detection, no-commits aider_no_output synthesis,
and park_terminal_sibling_tasks policy branches.
"""

from __future__ import annotations

import json
import logging
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
from tests._opencode_helpers import (
    earlier_stage_then_final_log,
    framed,
    write_opencode_log,
)
from tests._reconcile_helpers import (
    _stage_complete_payload,
    _write_staged_clients_yaml,
)
from tests.conftest import (
    _audit_failure_logged,
    _fail_audit_append,
    _make_daemon_session,
)


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

    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
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
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))

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


def test_local_harvest_audit_append_failure_still_persists_result_and_routes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconcile result-write path fails open (#2465): a broken audit inbox is
    logged, the synthesized result persists, and the task is routed exactly as
    it would be with a healthy inbox (the audit event changes no routing)."""
    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-audit-failure", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    session_id = "harv-audit-failure"
    sess = _mk_local_session(
        session_id,
        worktree,
        LocalLivenessHandle(pid=2_000_000_000, start_time_ns=11),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=session_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=session_id,
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(
        load_state(), list(task_by_ticket.values())
    )
    attempts = _fail_audit_append(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="cw.result"):
        harvested = _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 2, tzinfo=UTC),
            task_by_ticket=task_by_ticket,
        )

    assert harvested == [session_id]
    reloaded = next(s for s in load_state().sessions if s.id == session_id)
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.last_result is not None
    assert reloaded.last_result["status"] == "stage_complete"
    assert reloaded.last_result_source == LastResultSource.GIT_SYNTHESIS
    task = load_dev_queue().tasks[0]
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING
    assert _audit_failure_logged(
        caplog,
        session_id=session_id,
        source="git_synthesis",
        status="stage_complete",
    )
    assert attempts == [OrchestratorEventType.SESSION_RESULT_EMITTED]


def test_audit_existing_result_route_audit_failure_still_routes_task(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The second reconcile route into the audit helper (#2465): an
    ``audit_existing_result=True`` write (stalled sweep; conditionally idle and
    phantom) calls ``_record_result_emitted_audit`` directly, not through
    ``emit_result_on_audited``. A broken audit inbox is logged and the queue
    route proceeds exactly as with a healthy inbox."""
    from cw.reconcile._shared import _apply_sentinel_to_task_audited

    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    session_id = "audit-existing"
    payload = {**_stage_complete_payload(), "ticket_id": session_id}
    sentinel = AutoDevResult.model_validate(payload)
    sess = _make_daemon_session(
        id=session_id,
        name=f"client-a/auto-dev/{session_id}",
        stage=Stage.IMPL,
        last_result=payload,
        last_result_source=LastResultSource.SALVAGE_TRANSCRIPT,
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=session_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=session_id,
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    attempts = _fail_audit_append(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="cw.result"):
        audited = _apply_sentinel_to_task_audited(
            session_id,
            sess,
            sentinel,
            source=LastResultSource.SALVAGE_TRANSCRIPT,
            audit_existing_result=True,
        )

    assert audited.route is not None
    assert audited.route.routed is True
    assert audited.emit is None
    task = load_dev_queue().tasks[0]
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.PENDING
    assert sess.last_result == payload
    assert sess.last_result_source == LastResultSource.SALVAGE_TRANSCRIPT
    assert attempts == [OrchestratorEventType.SESSION_RESULT_EMITTED]
    assert _audit_failure_logged(
        caplog,
        session_id=session_id,
        source="salvage_transcript",
        status="stage_complete",
    )


def test_local_harvest_refused_by_door_leaves_session_and_task_untouched(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 0012 A3 (#1459): when the door refuses (a foreign terminal result is
    already recorded), the session-completion write is suppressed and no
    SESSION_COMPLETED or result-emitted audit event fires for the candidate,
    even with a broken audit inbox (#2465)."""
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
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    attempts = _fail_audit_append(monkeypatch)

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
    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "harv-refused"
    )
    assert task_after.status == QueueItemStatus.RUNNING
    assert task_after.stage == Stage.IMPL
    # No SESSION_COMPLETED event fired for the refused candidate.
    events = read_events(
        consumer="test-harvest-refused",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert not any(e.payload.get("session_id") == "harv-refused" for e in events)
    # A refusal never reaches the audit append, even with a broken inbox.
    assert attempts == []


def test_local_harvest_queue_save_failure_keeps_audit_event(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted emit audit is write-ahead of a failing queue save."""
    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-save-failure", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    session_id = "harv-save-failure"
    sess = _mk_local_session(
        session_id,
        worktree,
        LocalLivenessHandle(pid=2_000_000_000, start_time_ns=10),
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=session_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=session_id,
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(
        load_state(), list(task_by_ticket.values())
    )

    def _raise_save(_store: DevQueueStore) -> None:
        msg = "queue file unwritable"
        raise OSError(msg)

    monkeypatch.setattr("cw.reconcile._shared.save_dev_queue", _raise_save)
    with pytest.raises(OSError, match="queue file unwritable"):
        _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 2, tzinfo=UTC),
            task_by_ticket=task_by_ticket,
        )

    events = read_events(event_types=[OrchestratorEventType.SESSION_RESULT_EMITTED])
    assert len(events) == 1
    assert events[0].payload["session_id"] == session_id
    assert load_state().sessions[0].last_result is None
    assert load_dev_queue().tasks[0].status == QueueItemStatus.RUNNING


def test_act_on_local_harvest_candidates_completes_on_task_already_terminal(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """GitHub #2140: when the dev-queue task was already raced to a genuinely
    terminal status (COMPLETED/FAILED/CANCELLED) by a concurrent caller before
    this tick's lookup ran, ``_apply_sentinel_to_task`` reports
    ``routed=False, task_already_terminal=True``. Unlike idle.py/phantom.py,
    local.py's detect phase has no stamp/guard at all -- prior to this fix a
    ``not routed`` candidate here would ``continue`` unconditionally and
    re-synthesize the harvest sentinel (a real git subprocess call) on every
    subsequent tick forever, the worst-case orphan the ticket's investigation
    found. The already-terminal case must complete the session through the
    door instead, exactly like the ordinary path already does."""
    worktree = _local_git_worktree(
        make_git_repo, "wt-2140-harvest-terminal", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=42)
    sess = _mk_local_session("2140-harv-terminal", worktree, liveness)
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="2140-harv-terminal",
                    client="client-a",
                    status=QueueItemStatus.COMPLETED,
                    session_id="2140-harv-terminal",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}

    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    assert len(candidates) == 1

    harvested = _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    assert harvested == ["2140-harv-terminal"]
    reloaded = next(s for s in load_state().sessions if s.id == "2140-harv-terminal")
    assert reloaded.status == SessionStatus.COMPLETED

    task_after = next(
        t for t in load_dev_queue().tasks if t.ticket_id == "2140-harv-terminal"
    )
    assert task_after.status == QueueItemStatus.COMPLETED


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

    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
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

        candidates = _detect_local_harvest_candidates(state)

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

        candidates = _detect_local_harvest_candidates(state)

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

    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
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
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
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

    assert _detect_local_harvest_candidates(state) == []


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
    candidates = _detect_local_harvest_candidates(state)
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

    # The harvest compares the sentinel's ticket_id against the id in the
    # session name, which the executor builds as
    # ``{client}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}``
    # (src/cw/executor/core.py:386). The fixture's sentinel claims that same id,
    # and the queued row below carries it; what a real worker echoes back is
    # not established by this test (#2490, see ``_ticket_ids_match``).
    blocked = make_opencode_blocked(
        ticket_id="ses-oc-sentinel", worktree=worktree, reason="test-sentinel"
    )
    sentinel_json = blocked.model_dump_json()
    sentinel_text = f"<<<AUTO_DEV_RESULT\n{sentinel_json}\nAUTO_DEV_RESULT>>>"
    text_event = json.dumps({"type": "text", "part": {"text": sentinel_text}})
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text_event, encoding="utf-8")

    dead_handle = LocalLivenessHandle(pid=999999, start_time_ns=1, backend="opencode")
    sess = _mk_local_session("ses-oc-sentinel", worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="ses-oc-sentinel",
                    client="client-a",
                    stage=Stage.IMPL,
                    status=QueueItemStatus.RUNNING,
                    session_id="ses-oc-sentinel",
                )
            ]
        )
    )
    tasks = load_dev_queue().tasks

    candidates = _detect_local_harvest_candidates(load_state(), tasks)
    assert len(candidates) == 1

    with freezegun.freeze_time("2026-01-01 12:00:00"):
        _act_on_local_harvest_candidates(
            load_state(),
            candidates,
            now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            task_by_ticket={t.ticket_id: t for t in tasks},
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

    dead_handle = LocalLivenessHandle(pid=999999, start_time_ns=1, backend="opencode")
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


# ---------------------------------------------------------------------------
# #2369 — harvest dispatch keys on LocalLivenessHandle.backend, not log files
# ---------------------------------------------------------------------------


def _harvest_single(sid: str, ticket_id: str, worktree: Path, backend: str) -> Session:
    """Save one dead-handle session + RUNNING task, harvest it, return it."""
    dead_handle = LocalLivenessHandle.model_validate(
        {"pid": 2_000_000_000, "start_time_ns": 1, "backend": backend}
    )
    sess = _mk_local_session(sid, worktree, dead_handle)
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    stage=Stage.IMPL,
                    status=QueueItemStatus.RUNNING,
                    session_id=sid,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    state = load_state()
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    assert len(candidates) == 1
    _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )
    return next(s for s in load_state().sessions if s.id == sid)


def test_local_harvest_opencode_backend_without_log_routes_to_opencode(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """backend=opencode with NO .cw/opencode.log still routes through opencode
    synthesis (OPENCODE_NO_OUTPUT), never silently misroutes to git/aider."""
    worktree = _local_git_worktree(make_git_repo, "wt-oc-no-log", with_commit=False)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    assert not (worktree / OPENCODE_LOG_RELATIVE_PATH).exists()

    session = _harvest_single("ses-oc-no-log", "T-oc-no-log", worktree, "opencode")

    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NO_OUTPUT


def test_local_harvest_aider_backend_ignores_stray_opencode_log(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """backend=aider with a stray .cw/opencode.log still routes through git
    synthesis — the file's presence no longer drives dispatch."""
    worktree = _local_git_worktree(make_git_repo, "wt-aider-stray", with_commit=False)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("stale opencode output\n", encoding="utf-8")

    session = _harvest_single("ses-aider-stray", "T-aider-stray", worktree, "aider")

    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == "aider_no_output"


# ---------------------------------------------------------------------------
# #2490 -- last AUTO_DEV_RESULT wins; a refused dead-session harvest pages once
#
# Fixture provenance (composed arrangement, HYPOTHESIZED robustness lines, no
# capture of the failing session): see tests/_opencode_helpers.py.
# ---------------------------------------------------------------------------


def _save_dead_opencode_finalize(
    worktree: Path, ticket_id: str, stage: Stage
) -> dict[str, TicketTask]:
    """Save a dead opencode session + RUNNING row at *stage*; session id == ticket id.

    The session name is what ties a harvest candidate to its row
    (``ticket_id_for_session``), so the two ids must match.
    """
    sid = ticket_id
    dead_handle = LocalLivenessHandle(
        pid=2_000_000_000, start_time_ns=1, backend="opencode"
    )
    sess = _mk_local_session(sid, worktree, dead_handle)
    sess.stage = stage
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    stage=stage,
                    status=QueueItemStatus.RUNNING,
                    session_id=sid,
                )
            ]
        )
    )
    return {t.ticket_id: t for t in load_dev_queue().tasks}


def _attention_events(consumer: str, ticket_id: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        if e.payload.get("ticket_id") == ticket_id
    ]


def test_local_harvest_opencode_finalize_keeps_final_blocked_sentinel(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A finalize log quoting an earlier stage's sentinel still parks on the last.

    Before the fix the two blocks read as ``multiple_result_blocks``, the
    harvest result became ``opencode_no_output``, and the real disposition
    (``merge_gate_blocked``) and ``blocked_on_pr`` were lost (#2490 variant 2).
    """
    worktree = make_git_repo("wt-oc-final-wins")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    earlier = make_opencode_blocked(
        ticket_id="T-OC-F", worktree=worktree, reason="impl_failed"
    )
    final = make_opencode_blocked(
        ticket_id="T-OC-F",
        worktree=worktree,
        reason="prior_pipeline_pr_open",
        details="blocked by PR #2468 which is still open",
        retry_eligible=True,
        stage_reached="stage4a_merge_gate",
    ).model_copy(update={"status": "merge_gate_blocked"})
    write_opencode_log(worktree, earlier_stage_then_final_log(earlier, final))
    task_by_ticket = _save_dead_opencode_finalize(worktree, "T-OC-F", Stage.FINALIZE)
    state = load_state()
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))

    _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "T-OC-F")
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "merge_gate_blocked"
    assert task.blocked_on_pr == 2468
    session = next(s for s in load_state().sessions if s.id == "T-OC-F")
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result is not None
    assert session.last_result["blocker"]["reason"] == "prior_pipeline_pr_open"
    attention = _attention_events("test-oc-final-wins", "T-OC-F")
    assert [a["paused_status"] for a in attention] == ["merge_gate_blocked"]


def test_local_harvest_stage_mismatch_pages_once_and_stops_reoffering(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2490: a refused dead-process harvest latches and pages exactly once.

    Same stale-sentinel shape as the #1031 test above: git synthesis reports
    ``stage2_impl`` while the row sits at FINALIZE. Before the fix the refusal
    was silent and the dead candidate was re-offered (and re-refused, emitting
    another ``sentinel.stage_mismatch``) on every tick.
    """
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-page", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    save_state(CwState(sessions=[_mk_local_session("harv-page", worktree, liveness)]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-page",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-page",
                    stage=Stage.FINALIZE,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    now = datetime(2026, 1, 2, tzinfo=UTC)

    for _tick in range(3):
        state = load_state()
        candidates = _detect_local_harvest_candidates(
            state, list(task_by_ticket.values())
        )
        _act_on_local_harvest_candidates(
            state, candidates, now=now, task_by_ticket=task_by_ticket
        )

    attention = _attention_events("test-harvest-page", "harv-page")
    assert len(attention) == 1
    page = attention[0]
    assert page["paused_status"] == "sentinel_stage_mismatch_dead_session"
    assert page["session_id"] == "harv-page"
    assert page["client"] == "client-a"
    assert page["crashed"] is False
    assert page["lane"] == task_by_ticket["harv-page"].lane
    assert "stage_complete at stage2_impl" in str(page["breadcrumbs"])
    assert "finalize" in str(page["breadcrumbs"])
    mismatches = [
        e
        for e in read_events(
            consumer="test-harvest-page",
            event_types=[OrchestratorEventType.SENTINEL_STAGE_MISMATCH],
        )
        if e.payload.get("ticket_id") == "harv-page"
    ]
    assert len(mismatches) == 1
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "harv-page")
    assert task.status == QueueItemStatus.RUNNING
    session = next(s for s in load_state().sessions if s.id == "harv-page")
    assert session.status == SessionStatus.ACTIVE
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}
    assert (
        _detect_local_harvest_candidates(load_state(), list(task_by_ticket.values()))
        == []
    )


def test_local_harvest_non_stage_refusal_neither_pages_nor_latches(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A ``routed=False`` that is not a stage mismatch is left as it was (#2490).

    The row was requeued to PENDING while still carrying this session id: the
    lookup matches an excluded, non-terminal row, so ``routed`` is False for a
    reason the page and the latch must not claim is a stage mismatch.
    """
    worktree = _local_git_worktree(
        make_git_repo, "wt-harvest-pending", with_commit=True
    )
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    save_state(CwState(sessions=[_mk_local_session("harv-pend", worktree, liveness)]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-pend",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id="harv-pend",
                    stage=Stage.IMPL,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    state = load_state()
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    assert len(candidates) == 1

    harvested = _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    assert harvested == []
    assert _attention_events("test-harvest-pending", "harv-pend") == []
    session = next(s for s in load_state().sessions if s.id == "harv-pend")
    assert session.status == SessionStatus.ACTIVE
    assert session.last_result is None


def test_local_harvest_stage_mismatch_latch_merges_into_existing_last_result(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """An existing ``last_result`` dict keeps its own marker; the flag merges in."""
    worktree = _local_git_worktree(make_git_repo, "wt-harvest-merge", with_commit=True)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    sess = _mk_local_session("harv-merge", worktree, liveness)
    sess.last_result = {"paused_status": "silently_idle", "note": None}
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="harv-merge",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="harv-merge",
                    stage=Stage.REVIEW,
                )
            ]
        )
    )
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    state = load_state()
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    assert len(candidates) == 1

    _act_on_local_harvest_candidates(
        state,
        candidates,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        task_by_ticket=task_by_ticket,
    )

    session = next(s for s in load_state().sessions if s.id == "harv-merge")
    assert session.last_result == {
        "paused_status": "silently_idle",
        "note": None,
        "sentinel_advance_refused": True,
    }
    assert (
        _detect_local_harvest_candidates(load_state(), list(task_by_ticket.values()))
        == []
    )
    assert len(_attention_events("test-harvest-merge", "harv-merge")) == 1


def _with_recovery_hint(result: AutoDevResult, hint: str) -> AutoDevResult:
    """*result* with its blocker's ``recovery_hint`` set."""
    assert result.blocker is not None
    blocker = result.blocker.model_copy(update={"recovery_hint": hint})
    return result.model_copy(update={"blocker": blocker})


def test_stage_mismatch_attention_payload_names_the_blocker_reason(
    tmp_path: Path,
) -> None:
    """A refused ``blocked`` result's breadcrumbs carry its blocker reason."""
    from cw.reconcile.local import _stage_mismatch_attention_payload

    sentinel = _with_recovery_hint(
        make_opencode_blocked(
            ticket_id="T-1",
            worktree=tmp_path,
            reason="merge_conflict_post_push",
            stage_reached="stage4b_pr_create",
        ),
        "rebase onto main then requeue",
    )
    session = _mk_local_session(
        "ses-payload",
        tmp_path,
        LocalLivenessHandle(pid=1, start_time_ns=1, backend="opencode"),
    )
    task = TicketTask(ticket_id="T-1", client="client-a", stage=Stage.IMPL)

    payload = _stage_mismatch_attention_payload(
        session, task, sentinel, "opencode", Stage.FINALIZE
    )

    breadcrumbs = str(payload["breadcrumbs"])
    assert "blocked at stage4b_pr_create (merge_conflict_post_push" in breadcrumbs
    assert "rebase onto main then requeue" in breadcrumbs
    assert "dead opencode process" in breadcrumbs
    # The live row stage passed in, not the (stale) snapshot's IMPL.
    assert "the row is at stage finalize" in breadcrumbs
    assert "cw spawn close --confirmed-dead --requeue ses-payload" in breadcrumbs
    assert payload["ticket_id"] == "T-1"
    assert payload["claude_session_id"] is None


# ---------------------------------------------------------------------------
# #2490 review fixes -- page-before-latch, latch scope, end-to-end opencode
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _save_refusal_scenario(
    make_git_repo: Callable[[str], Path],
    tmp_config_dir: Path,
    rows: dict[str, Stage],
) -> dict[str, TicketTask]:
    """Dead aider sessions (id == ticket id) + RUNNING rows at the given stages.

    A row at FINALIZE refuses the git-synthesized ``stage_complete`` at
    ``stage2_impl`` (stale advance claim); a row at IMPL accepts it.
    """
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    liveness = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=123)
    sessions = []
    tasks = []
    for ticket, stage in rows.items():
        worktree = _local_git_worktree(make_git_repo, f"wt-{ticket}", with_commit=True)
        sessions.append(_mk_local_session(ticket, worktree, liveness))
        tasks.append(
            TicketTask(
                ticket_id=ticket,
                client="client-a",
                status=QueueItemStatus.RUNNING,
                session_id=ticket,
                stage=stage,
            )
        )
    save_state(CwState(sessions=sessions))
    save_dev_queue(DevQueueStore(tasks=tasks))
    return {t.ticket_id: t for t in load_dev_queue().tasks}


def _harvest_tick(task_by_ticket: dict[str, TicketTask]) -> list[str]:
    """One detect + act pass over the persisted state."""
    state = load_state()
    candidates = _detect_local_harvest_candidates(state, list(task_by_ticket.values()))
    return _act_on_local_harvest_candidates(
        state, candidates, now=_NOW, task_by_ticket=task_by_ticket
    )


def _failing_record_event(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_type: OrchestratorEventType,
    fail_for: Callable[[dict[str, object]], bool],
) -> list[int]:
    """Make ``cw.reconcile.local.record_event`` raise OSError on matching calls.

    Returns a one-element-per-failure list so a test can count the failures.
    """
    from cw.events import record_event as real_record_event

    failures: list[int] = []

    def flaky(
        etype: OrchestratorEventType,
        payload: dict[str, object] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> object:
        if etype is event_type and fail_for(payload or {}):
            failures.append(1)
            msg = "disk full"
            raise OSError(msg)
        return real_record_event(etype, payload, correlation_id=correlation_id)

    monkeypatch.setattr("cw.reconcile.local.record_event", flaky)
    return failures


def _session(sid: str) -> Session:
    return next(s for s in load_state().sessions if s.id == sid)


def test_failed_page_leaves_the_session_unlatched_and_repages_next_tick(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never latch a session whose page was not emitted (#2490 review).

    Latch-before-page left a session skipped forever with no page when the page
    write failed. The page is emitted first; the latch follows only on success.
    """
    tbt = _save_refusal_scenario(
        make_git_repo, tmp_config_dir, {"pg-fail": Stage.FINALIZE}
    )
    writes_fail = {"on": True}
    failures = _failing_record_event(
        monkeypatch,
        event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        fail_for=lambda _payload: writes_fail["on"],
    )

    _harvest_tick(tbt)

    assert failures == [1]
    assert _attention_events("test-pg-fail-a", "pg-fail") == []
    assert _session("pg-fail").last_result is None
    assert len(_detect_local_harvest_candidates(load_state(), list(tbt.values()))) == 1

    writes_fail["on"] = False
    _harvest_tick(tbt)
    _harvest_tick(tbt)

    assert len(_attention_events("test-pg-fail-b", "pg-fail")) == 1
    assert _session("pg-fail").last_result == {
        "paused_status": "sentinel_stage_mismatch_refused"
    }
    assert _detect_local_harvest_candidates(load_state(), list(tbt.values())) == []


def test_one_failing_page_does_not_cancel_another_sessions_page(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tbt = _save_refusal_scenario(
        make_git_repo,
        tmp_config_dir,
        {"pg-one": Stage.FINALIZE, "pg-two": Stage.FINALIZE},
    )
    _failing_record_event(
        monkeypatch,
        event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        fail_for=lambda payload: payload.get("ticket_id") == "pg-one",
    )

    _harvest_tick(tbt)

    assert _attention_events("test-pg-multi", "pg-one") == []
    assert len(_attention_events("test-pg-multi", "pg-two")) == 1
    assert _session("pg-one").last_result is None
    assert _session("pg-two").last_result == {
        "paused_status": "sentinel_stage_mismatch_refused"
    }


def test_failing_session_completed_write_does_not_suppress_another_sessions_page(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page goes out before any SESSION_COMPLETED write can abort the pass."""
    tbt = _save_refusal_scenario(
        make_git_repo,
        tmp_config_dir,
        {"pg-done": Stage.IMPL, "pg-refused": Stage.FINALIZE},
    )
    failures = _failing_record_event(
        monkeypatch,
        event_type=OrchestratorEventType.SESSION_COMPLETED,
        fail_for=lambda _payload: True,
    )

    with pytest.raises(OSError, match="disk full"):
        _harvest_tick(tbt)

    assert failures == [1]
    assert len(_attention_events("test-pg-completed", "pg-refused")) == 1
    assert _session("pg-refused").last_result == {
        "paused_status": "sentinel_stage_mismatch_refused"
    }
    assert _session("pg-done").status == SessionStatus.COMPLETED


def test_latched_session_whose_row_moved_on_is_reoffered_and_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A latch is honored only while the row is still bound to the session (#2490)."""
    tbt = _save_refusal_scenario(
        make_git_repo, tmp_config_dir, {"lt-moved": Stage.FINALIZE}
    )
    _harvest_tick(tbt)
    assert _detect_local_harvest_candidates(load_state(), list(tbt.values())) == []

    # The operator requeued the row: PENDING, session id cleared.
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="lt-moved",
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    stage=Stage.FINALIZE,
                )
            ]
        )
    )
    moved = {t.ticket_id: t for t in load_dev_queue().tasks}
    assert (
        len(_detect_local_harvest_candidates(load_state(), list(moved.values()))) == 1
    )

    _harvest_tick(moved)

    assert _session("lt-moved").status == SessionStatus.COMPLETED
    assert len(_attention_events("test-lt-moved", "lt-moved")) == 1


def test_latch_is_honored_while_a_parked_row_is_still_bound_to_the_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A parked row still holding the session id keeps the latch (no re-page)."""
    tbt = _save_refusal_scenario(
        make_git_repo, tmp_config_dir, {"lt-parked": Stage.FINALIZE}
    )
    _harvest_tick(tbt)
    parked = tbt["lt-parked"].model_copy(
        update={"status": QueueItemStatus.BLOCKED_ON_USER}
    )

    assert _detect_local_harvest_candidates(load_state(), [parked]) == []


def test_old_sessions_latch_does_not_affect_a_new_session_for_the_same_ticket(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Two dead sessions for one ticket: only the un-latched one is offered."""
    tbt = _save_refusal_scenario(
        make_git_repo, tmp_config_dir, {"lt-same": Stage.FINALIZE}
    )
    old = _session("lt-same")
    old.last_result = {"paused_status": "sentinel_stage_mismatch_refused"}
    assert old.worktree_path is not None
    assert old.local_liveness is not None
    fresh = _mk_local_session("lt-same-new", old.worktree_path, old.local_liveness)
    fresh.name = old.name
    save_state(CwState(sessions=[old, fresh]))

    candidates = _detect_local_harvest_candidates(load_state(), list(tbt.values()))

    assert [c.session_id for c in candidates] == ["lt-same-new"]


def test_local_harvest_opencode_refused_result_pages_with_blocker_and_recovery(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """End to end: a refused opencode ``blocked`` result pages once, losing nothing.

    The dead opencode worker's log ends in a ``blocked`` sentinel the shared guard
    refuses (the row's client is not configured, so the stage position is
    unresolvable and every non-matching stage refuses). The page must carry what
    the worker reported (status, stage, blocker reason, its recovery hint), the
    row's LIVE stage -- the per-pass snapshot handed in is deliberately stale --
    and the exact recovery command.
    """
    worktree = make_git_repo("wt-oc-refused")
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    reported = _with_recovery_hint(
        make_opencode_blocked(
            ticket_id="oc-refused",
            worktree=worktree,
            reason="merge_conflict_post_push",
            stage_reached="stage2_impl",
        ),
        "rebase onto main then requeue",
    )
    write_opencode_log(worktree, [{"type": "text", "part": {"text": framed(reported)}}])
    dead = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1, backend="opencode")
    save_state(CwState(sessions=[_mk_local_session("oc-refused", worktree, dead)]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="oc-refused",
                    client="client-unconfigured",
                    status=QueueItemStatus.RUNNING,
                    session_id="oc-refused",
                    stage=Stage.FINALIZE,
                )
            ]
        )
    )
    stale = {
        t.ticket_id: t.model_copy(update={"stage": Stage.IMPL})
        for t in load_dev_queue().tasks
    }

    _harvest_tick(stale)
    _harvest_tick(stale)

    pages = _attention_events("test-oc-refused", "oc-refused")
    assert len(pages) == 1
    assert pages[0]["paused_status"] == "sentinel_stage_mismatch_dead_session"
    breadcrumbs = str(pages[0]["breadcrumbs"])
    assert "blocked at stage2_impl (merge_conflict_post_push" in breadcrumbs
    assert "rebase onto main then requeue" in breadcrumbs
    assert "the row is at stage finalize" in breadcrumbs
    assert "cw spawn close --confirmed-dead --requeue oc-refused" in breadcrumbs
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == "oc-refused")
    assert task.status == QueueItemStatus.RUNNING
    assert _session("oc-refused").status == SessionStatus.ACTIVE


def _latched_session_and_its_row(
    make_git_repo: Callable[[str], Path],
    tmp_config_dir: Path,
    ticket: str,
) -> TicketTask:
    """Latch a refused dead session (id == *ticket*); return its RUNNING row."""
    tbt = _save_refusal_scenario(
        make_git_repo, tmp_config_dir, {ticket: Stage.FINALIZE}
    )
    _harvest_tick(tbt)
    assert _session(ticket).last_result == {
        "paused_status": "sentinel_stage_mismatch_refused"
    }
    return tbt[ticket]


def test_latch_is_honored_when_the_occupied_row_is_not_the_last_for_the_ticket(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Duplicate rows for one ticket id: the router's match decides, not row order.

    ``add`` after a terminal row leaves two rows for one ``(client, ticket_id)``.
    A ticket-id-keyed dict keeps only the LAST, which here is not the row that
    owns the session: the latch would be ignored, the session re-offered, and the
    router would re-refuse and re-page it every tick.
    """
    owning = _latched_session_and_its_row(make_git_repo, tmp_config_dir, "lt-dup")
    later_duplicate = TicketTask(
        ticket_id="lt-dup", client="client-a", status=QueueItemStatus.PENDING
    )

    candidates = _detect_local_harvest_candidates(
        load_state(), [owning, later_duplicate]
    )

    assert candidates == []


def test_latch_is_dropped_when_the_row_is_bound_to_a_newer_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """An old latched dead session whose row now belongs to another session is offered.

    The row is RUNNING (occupied) but bound to a different, newer session id: the
    refusal the latch recorded no longer describes this session's row, so the
    session must complete the ordinary way instead of lingering ACTIVE.
    """
    owning = _latched_session_and_its_row(make_git_repo, tmp_config_dir, "lt-newer")
    rebound = owning.model_copy(update={"session_id": "lt-newer-session-2"})

    candidates = _detect_local_harvest_candidates(load_state(), [rebound])

    assert [c.session_id for c in candidates] == ["lt-newer"]
