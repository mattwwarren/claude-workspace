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

from cw._config_migrate import migrate_cw_state
from cw._git import run_git
from cw.auto_dev_result import AutoDevResult
from cw.config import (
    load_state,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.local_runner import AIDER_LOG_RELATIVE_PATH
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    LastResultSource,
    LocalLivenessBackend,
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
from cw.reconcile.harvest_synthesis import _resolve_harvest_backend
from tests._clients_yaml import staged_client, write_clients_yaml
from tests._opencode_helpers import (
    earlier_stage_then_final_log,
    framed,
    text_event,
    write_opencode_log,
)
from tests._reconcile_helpers import (
    _attention_events,
    _failing_record_event,
    _stage_complete_payload,
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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

    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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

    monkeypatch.setattr("cw.reconcile._shared._routing.save_dev_queue", _raise_save)
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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

    monkeypatch.setattr("cw.reconcile.harvest_synthesis.synthesize_git_result", _spy)
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))

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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))

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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    synthesis — the file's presence no longer drives dispatch.

    ``.cw/aider.log`` is also present: the state a real aider run always leaves
    (the launch opens it before ``Popen``), which is what makes the stray
    opencode log mere noise rather than evidence of an opencode launch (#2512).
    """
    worktree = _local_git_worktree(make_git_repo, "wt-aider-stray", with_commit=False)
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("stale opencode output\n", encoding="utf-8")
    _touch_aider_log(worktree)

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


def _touch_aider_log(worktree: Path) -> None:
    """Leave the empty ``.cw/aider.log`` a real aider launch always leaves (#2512)."""
    log_path = worktree / AIDER_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")


def _legacy_handle_state(sess: Session) -> CwState:
    """*sess* as a pre-#2369 on-disk record run through the real v18->v19 migration.

    The handle has no ``backend`` on disk (schema 18); the migration writes an
    explicit ``"aider"`` that is byte-identical to a genuine aider handle -- the
    incident shape of #2512.
    """
    raw = CwState(sessions=[sess]).model_dump(mode="json")
    raw["schema_version"] = 18
    del raw["sessions"][0]["local_liveness"]["backend"]
    migrated = migrate_cw_state(raw)
    assert migrated["sessions"][0]["local_liveness"]["backend"] == "aider"
    return CwState.model_validate(migrated)


def _save_dead_local_session(
    worktree: Path,
    ticket_id: str,
    *,
    backend: str | None,
    session_stage: Stage | None,
    row_stage: Stage,
    row_client: str = "client-a",
) -> dict[str, TicketTask]:
    """Save a dead local session + RUNNING row at *row_stage*; session id == ticket id.

    The session name is what ties a harvest candidate to its row
    (``ticket_id_for_session``), so the two ids must match. *backend* ``None``
    persists a LEGACY handle (see :func:`_legacy_handle_state`); otherwise the
    handle records *backend* explicitly. *session_stage* is the spawn stage
    ``Session.stage`` stamps, *row_stage* the dev-queue row's current stage. The
    client is written first (stage list + ``sentinel_mismatch_veto``) so the stage
    position resolves; ``client-unconfigured`` skips it so the position does not.
    """
    if row_client == "client-a":
        write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    sid = ticket_id
    dead_handle = LocalLivenessHandle.model_validate(
        {"pid": 2_000_000_000, "start_time_ns": 1, "backend": backend or "aider"}
    )
    sess = _mk_local_session(sid, worktree, dead_handle)
    sess.stage = session_stage
    save_state(CwState(sessions=[sess]) if backend else _legacy_handle_state(sess))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client=row_client,
                    stage=row_stage,
                    status=QueueItemStatus.RUNNING,
                    session_id=sid,
                )
            ]
        )
    )
    return {t.ticket_id: t for t in load_dev_queue().tasks}


def _save_dead_opencode_finalize(
    worktree: Path, ticket_id: str, stage: Stage
) -> dict[str, TicketTask]:
    """Save a dead opencode session + RUNNING row at *stage* (id == ticket id)."""
    return _save_dead_local_session(
        worktree,
        ticket_id,
        backend="opencode",
        session_stage=stage,
        row_stage=stage,
    )


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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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


# Where each record_event the local harvest reaches is looked up: the page is
# emitted by the shared _stage_refusal module, SESSION_COMPLETED by local itself.
_PAGE_RECORD_EVENT = "cw.reconcile._shared._stage_refusal.record_event"
_LOCAL_RECORD_EVENT = "cw.reconcile.local.record_event"


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
        target=_PAGE_RECORD_EVENT,
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
        target=_PAGE_RECORD_EVENT,
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
        target=_LOCAL_RECORD_EVENT,
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
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
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


# ---------------------------------------------------------------------------
# #2512 -- a recorded "aider" backend is verified at harvest, not trusted
#
# A pre-#2369 handle migrates to an explicit "aider" byte-identical to a genuine
# aider handle, so only the launch logs and the session's spawn stage can tell
# them apart. A backend that cannot be proven parks the row; it is never guessed.
# ---------------------------------------------------------------------------

_UNEXPECTED_ERROR = "unexpected_error"
_LOCAL_NEXT_ACTIONS = ["user_resolve_local_executor_failure"]
_OPENCODE_NEXT_ACTIONS = ["user_resolve_opencode_executor_failure"]
# row/spawn stage -> the entry marker a failure sentinel must carry.
_PARK_STAGES = [
    pytest.param(Stage.FINALIZE, "stage4a_merge_gate", id="finalize"),
    pytest.param(Stage.REVIEW, "stage3_review", id="review"),
    pytest.param(Stage.PLAN, "stage1_plan", id="plan"),
]


def _last_result(sid: str) -> AutoDevResult:
    return AutoDevResult.model_validate(_session(sid).last_result)


def _row(ticket_id: str) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _stage_mismatch_events(ticket_id: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=f"test-mismatch-{ticket_id}",
            event_types=[OrchestratorEventType.SENTINEL_STAGE_MISMATCH],
        )
        if e.payload.get("ticket_id") == ticket_id
    ]


def _override_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "cw.reconcile.local"
        and "harvest_backend_overridden" in r.getMessage()
    ]


def _fallback_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "cw.reconcile.local"
        and "harvest_synthesis_failed" in r.getMessage()
    ]


def _forbid_synthesis_call(**_kwargs: object) -> object:
    msg = "this result synthesizer must not run for this harvest"
    raise AssertionError(msg)


def _forbid_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make either result synthesizer fail the test if it is ever called."""
    monkeypatch.setattr(
        "cw.reconcile.harvest_synthesis.synthesize_opencode_result",
        _forbid_synthesis_call,
    )
    monkeypatch.setattr(
        "cw.reconcile.harvest_synthesis.synthesize_git_result", _forbid_synthesis_call
    )


def _decoy_opencode_log(worktree: Path, ticket_id: str) -> None:
    """An opencode log whose parseable sentinel must NOT be consulted."""
    decoy = make_opencode_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason="decoy_opencode_result",
        stage_reached="stage4a_merge_gate",
    ).model_copy(update={"status": "merge_gate_blocked"})
    write_opencode_log(worktree, [text_event(framed(decoy))])


def _arrange_launch_logs(worktree: Path, logs: str, ticket_id: str) -> None:
    """Leave the launch logs *logs* names: aider_only, opencode_only, both, neither."""
    if logs in ("opencode_only", "both"):
        _decoy_opencode_log(worktree, ticket_id)
    if logs in ("aider_only", "both"):
        _touch_aider_log(worktree)


def _assert_parked_unproven(
    ticket_id: str, stage: Stage, marker: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The unproven-backend outcome: blocked at the row's marker, parked and paged."""
    result = _last_result(ticket_id)
    assert result.status == "blocked"
    assert result.stage_reached == marker
    assert result.stage_reached != "stage2_impl" or stage is Stage.IMPL
    assert result.blocker is not None
    assert result.blocker.stage == marker
    assert result.blocker.reason == _UNEXPECTED_ERROR
    assert "could not be proven" in result.blocker.details
    assert result.blocker.retry_eligible is None
    assert result.next_actions == _LOCAL_NEXT_ACTIONS
    assert result.scope.lines_actual == (None if marker == "stage1_plan" else 0)
    row = _row(ticket_id)
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == "blocked"
    assert row.blocked_reason == _UNEXPECTED_ERROR
    assert row.stage == stage
    assert _session(ticket_id).status == SessionStatus.COMPLETED
    assert _stage_mismatch_events(ticket_id) == []
    pages = _attention_events(f"test-park-{ticket_id}", ticket_id)
    assert [p["paused_status"] for p in pages] == ["blocked"]
    assert _UNEXPECTED_ERROR in str(pages[0]["breadcrumbs"])
    assert _override_warnings(caplog) == []


def test_legacy_handle_only_opencode_log_harvests_via_opencode_not_git(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The #2512 incident: a legacy handle + only .cw/opencode.log is an opencode run.

    Before the fix the migrated ``"aider"`` sent it down git synthesis, which
    stamped ``stage2_impl`` on a FINALIZE row; the stage guard refused it and the
    worker's real merge-gate result was dropped.
    """
    worktree = make_git_repo("wt-leg-oc")
    final = make_opencode_blocked(
        ticket_id="T-LEG-OC",
        worktree=worktree,
        reason="prior_pipeline_pr_open",
        details="blocked by PR #2468 which is still open",
        retry_eligible=True,
        stage_reached="stage4a_merge_gate",
    ).model_copy(update={"status": "merge_gate_blocked"})
    write_opencode_log(worktree, [text_event(framed(final))])
    tbt = _save_dead_local_session(
        worktree,
        "T-LEG-OC",
        backend=None,
        session_stage=Stage.FINALIZE,
        row_stage=Stage.FINALIZE,
    )

    with caplog.at_level(logging.WARNING, logger="cw.reconcile.local"):
        _harvest_tick(tbt)

    result = _last_result("T-LEG-OC")
    assert result.stage_reached == "stage4a_merge_gate"
    assert result.stage_reached != "stage2_impl"
    assert result.status == "merge_gate_blocked"
    assert result.blocker is not None
    assert result.blocker.reason == "prior_pipeline_pr_open"
    row = _row("T-LEG-OC")
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.disposition == "merge_gate_blocked"
    assert row.blocked_on_pr == 2468
    assert _session("T-LEG-OC").status == SessionStatus.COMPLETED
    assert _stage_mismatch_events("T-LEG-OC") == []
    pages = _attention_events("test-leg-oc", "T-LEG-OC")
    assert [p["paused_status"] for p in pages] == ["merge_gate_blocked"]
    warnings = _override_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "T-LEG-OC" in message
    assert "#2512" in message


@pytest.mark.parametrize("logs", ["aider_only", "both", "neither"])
def test_legacy_handle_impl_spawn_stage_git_synthesizes(
    logs: str,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An IMPL spawn keeps git synthesis whatever the logs say (stage-correct there)."""
    ticket = f"T-LEG-IMPL-{logs}"
    worktree = _local_git_worktree(make_git_repo, f"wt-{ticket}", with_commit=True)
    _arrange_launch_logs(worktree, logs, ticket)
    tbt = _save_dead_local_session(
        worktree,
        ticket,
        backend=None,
        session_stage=Stage.IMPL,
        row_stage=Stage.IMPL,
    )
    monkeypatch.setattr(
        "cw.reconcile.harvest_synthesis.synthesize_opencode_result",
        _forbid_synthesis_call,
    )

    _harvest_tick(tbt)

    result = _last_result(ticket)
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage2_impl"
    assert result.commits
    assert _session(ticket).status == SessionStatus.COMPLETED
    assert _stage_mismatch_events(ticket) == []
    assert _override_warnings(caplog) == []


def test_legacy_handle_only_aider_log_non_impl_session_still_refused_and_paged(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only aider.log on a FINALIZE spawn keeps git synthesis and is refused + paged.

    Pins that this PR does not silently reroute a misconfigured ``local`` backend:
    the stage guard still refuses the ``stage2_impl`` claim, exactly as before.
    """
    worktree = _local_git_worktree(make_git_repo, "wt-leg-aider", with_commit=True)
    _touch_aider_log(worktree)
    tbt = _save_dead_local_session(
        worktree,
        "T-LEG-AIDER",
        backend=None,
        session_stage=Stage.FINALIZE,
        row_stage=Stage.FINALIZE,
    )

    _harvest_tick(tbt)

    mismatches = _stage_mismatch_events("T-LEG-AIDER")
    assert len(mismatches) == 1
    assert mismatches[0]["sentinel_stage_reached"] == "stage2_impl"
    pages = _attention_events("test-leg-aider", "T-LEG-AIDER")
    assert len(pages) == 1
    assert pages[0]["paused_status"] == "sentinel_stage_mismatch_dead_session"
    breadcrumbs = str(pages[0]["breadcrumbs"])
    assert "dead aider process" in breadcrumbs
    assert "stage_complete at stage2_impl" in breadcrumbs
    assert _row("T-LEG-AIDER").status == QueueItemStatus.RUNNING
    assert _row("T-LEG-AIDER").stage == Stage.FINALIZE
    session = _session("T-LEG-AIDER")
    assert session.status == SessionStatus.ACTIVE
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}
    assert _override_warnings(caplog) == []


@pytest.mark.parametrize("logs", ["both", "neither"])
@pytest.mark.parametrize(("stage", "marker"), _PARK_STAGES)
def test_legacy_handle_unproven_backend_parks_at_row_stage(
    logs: str,
    stage: Stage,
    marker: str,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both-or-neither logs on a non-IMPL spawn: backend unproven, so the row parks.

    Neither synthesizer runs (so no log contents are consulted) and the blocked
    result carries the row's own entry marker -- never a later one that would walk
    the pointer, never ``stage2_impl`` that the guard would refuse.
    """
    ticket = f"T-PARK-{logs}-{stage.value}"
    worktree = _local_git_worktree(make_git_repo, f"wt-{ticket}", with_commit=True)
    _arrange_launch_logs(worktree, logs, ticket)
    tbt = _save_dead_local_session(
        worktree,
        ticket,
        backend=None,
        session_stage=stage,
        row_stage=stage,
    )
    _forbid_synthesis(monkeypatch)

    _harvest_tick(tbt)

    _assert_parked_unproven(ticket, stage, marker, caplog)


@pytest.mark.parametrize(
    ("logs", "row_stage", "marker"),
    [
        pytest.param("both", Stage.FINALIZE, "stage4a_merge_gate", id="both-fin"),
        pytest.param("neither", Stage.REVIEW, "stage3_review", id="none-rev"),
        pytest.param("neither", Stage.PLAN, "stage1_plan", id="none-plan"),
        pytest.param("both", Stage.IMPL, "stage2_impl", id="both-impl"),
    ],
)
def test_legacy_handle_session_stage_none_decides_by_row_stage(
    logs: str,
    row_stage: Stage,
    marker: str,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no spawn stage on the session the row's stage is the fallback.

    An IMPL row keeps git synthesis; any other row parks, exactly as when the
    session carries the spawn stage.
    """
    expect_git = row_stage is Stage.IMPL
    ticket = f"T-NOSTAGE-{logs}-{row_stage.value}"
    worktree = _local_git_worktree(make_git_repo, f"wt-{ticket}", with_commit=True)
    _arrange_launch_logs(worktree, logs, ticket)
    tbt = _save_dead_local_session(
        worktree,
        ticket,
        backend=None,
        session_stage=None,
        row_stage=row_stage,
    )
    if expect_git:
        monkeypatch.setattr(
            "cw.reconcile.harvest_synthesis.synthesize_opencode_result",
            _forbid_synthesis_call,
        )
    else:
        _forbid_synthesis(monkeypatch)

    _harvest_tick(tbt)

    if expect_git:
        result = _last_result(ticket)
        assert result.status == "stage_complete"
        assert result.stage_reached == marker
    else:
        _assert_parked_unproven(ticket, row_stage, marker, caplog)


def test_legacy_handle_session_stage_none_synthetic_impl_task_git_synthesizes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No queue row + no spawn stage: the synthetic task is IMPL, so git synthesis."""
    worktree = _local_git_worktree(make_git_repo, "wt-synth-impl", with_commit=True)
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    sess = _mk_local_session(
        "T-SYNTH",
        worktree,
        LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1),
    )
    sess.stage = None
    save_state(_legacy_handle_state(sess))

    state = load_state()
    candidates = _detect_local_harvest_candidates(state)
    _act_on_local_harvest_candidates(state, candidates, now=_NOW, task_by_ticket={})

    last_result = _session("T-SYNTH").last_result
    assert last_result is not None
    assert last_result["status"] == "stage_complete"
    assert last_result["stage_reached"] == "stage2_impl"


def test_recorded_opencode_with_both_logs_non_impl_still_uses_opencode_log(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recorded opencode backend is trusted: both logs on FINALIZE change nothing."""
    worktree = make_git_repo("wt-rec-oc")
    _touch_aider_log(worktree)
    final = make_opencode_blocked(
        ticket_id="T-REC-OC",
        worktree=worktree,
        reason="prior_pipeline_pr_open",
        details="blocked by PR #2468 which is still open",
        retry_eligible=True,
        stage_reached="stage4a_merge_gate",
    ).model_copy(update={"status": "merge_gate_blocked"})
    write_opencode_log(worktree, [text_event(framed(final))])
    tbt = _save_dead_local_session(
        worktree,
        "T-REC-OC",
        backend="opencode",
        session_stage=Stage.FINALIZE,
        row_stage=Stage.FINALIZE,
    )

    _harvest_tick(tbt)

    result = _last_result("T-REC-OC")
    assert result.status == "merge_gate_blocked"
    assert result.stage_reached == "stage4a_merge_gate"
    assert _override_warnings(caplog) == []


@pytest.mark.parametrize("backend", ["opencode", "codex"])
def test_resolve_harvest_backend_passes_non_aider_through(
    backend: LocalLivenessBackend, tmp_path: Path
) -> None:
    """opencode / codex are returned as recorded, without touching the worktree."""
    worktree = tmp_path / "nonexistent"
    sess = _mk_local_session(
        "s-pass",
        worktree,
        LocalLivenessHandle(pid=1, start_time_ns=1, backend=backend),
    )
    task = TicketTask(ticket_id="T-PASS", client="client-a", stage=Stage.FINALIZE)

    assert _resolve_harvest_backend(backend, sess, task, worktree) == backend


# (opencode.log, aider.log, session stage, row stage, expected effective backend)
_RULE_TABLE = [
    pytest.param(True, False, Stage.IMPL, Stage.IMPL, "opencode", id="oc-only-impl"),
    pytest.param(
        True, False, Stage.FINALIZE, Stage.FINALIZE, "opencode", id="oc-only-fin"
    ),
    pytest.param(
        False, True, Stage.FINALIZE, Stage.FINALIZE, "aider", id="aider-only-fin"
    ),
    pytest.param(False, True, Stage.IMPL, Stage.IMPL, "aider", id="aider-only-impl"),
    pytest.param(True, True, Stage.IMPL, Stage.IMPL, "aider", id="both-impl"),
    pytest.param(True, True, Stage.IMPL, Stage.FINALIZE, "aider", id="both-spawn-impl"),
    pytest.param(False, False, Stage.IMPL, Stage.IMPL, "aider", id="none-impl"),
    pytest.param(
        False, False, Stage.IMPL, Stage.REVIEW, "aider", id="none-spawn-impl-row-rev"
    ),
    pytest.param(True, True, Stage.PLAN, Stage.PLAN, None, id="both-plan"),
    pytest.param(True, True, Stage.REVIEW, Stage.REVIEW, None, id="both-review"),
    pytest.param(True, True, Stage.FINALIZE, Stage.FINALIZE, None, id="both-final"),
    pytest.param(True, True, Stage.HARDEN, Stage.HARDEN, None, id="both-harden"),
    pytest.param(False, False, Stage.PLAN, Stage.PLAN, None, id="none-plan"),
    pytest.param(False, False, Stage.REVIEW, Stage.REVIEW, None, id="none-review"),
    pytest.param(False, False, Stage.FINALIZE, Stage.FINALIZE, None, id="none-final"),
    pytest.param(False, False, Stage.HARDEN, Stage.HARDEN, None, id="none-harden"),
    pytest.param(True, True, None, Stage.FINALIZE, None, id="both-nostage-final"),
    pytest.param(False, False, None, Stage.REVIEW, None, id="none-nostage-review"),
    pytest.param(True, True, None, Stage.IMPL, "aider", id="both-nostage-impl"),
    pytest.param(False, False, None, Stage.IMPL, "aider", id="none-nostage-impl"),
]


@pytest.mark.parametrize(
    ("opencode_log", "aider_log", "session_stage", "row_stage", "expected"),
    _RULE_TABLE,
)
def test_resolve_harvest_backend_rule_table(
    opencode_log: bool,
    aider_log: bool,
    session_stage: Stage | None,
    row_stage: Stage,
    expected: str | None,
    tmp_path: Path,
) -> None:
    """Every row of the recorded-``aider`` behavior matrix (#2512)."""
    logs = {
        (True, False): "opencode_only",
        (False, True): "aider_only",
        (True, True): "both",
        (False, False): "neither",
    }[(opencode_log, aider_log)]
    _arrange_launch_logs(tmp_path, logs, "T-RT")
    sess = _mk_local_session(
        "s-rt", tmp_path, LocalLivenessHandle(pid=1, start_time_ns=1)
    )
    sess.stage = session_stage
    task = TicketTask(ticket_id="T-RT", client="client-a", stage=row_stage)

    assert _resolve_harvest_backend("aider", sess, task, tmp_path) == expected


def test_resolve_harvest_backend_oserror_probe_reads_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that raises ``OSError`` counts as 'log absent', never as a crash."""

    def _boom(*_args: object, **_kwargs: object) -> bool:
        msg = "permission denied"
        raise OSError(msg)

    monkeypatch.setattr(Path, "exists", _boom)
    sess = _mk_local_session(
        "s-oserr", tmp_path, LocalLivenessHandle(pid=1, start_time_ns=1)
    )
    impl = TicketTask(ticket_id="T-OS", client="client-a", stage=Stage.IMPL)
    plan = TicketTask(ticket_id="T-OS", client="client-a", stage=Stage.PLAN)

    sess.stage = Stage.IMPL
    assert _resolve_harvest_backend("aider", sess, impl, tmp_path) == "aider"
    sess.stage = Stage.PLAN
    assert _resolve_harvest_backend("aider", sess, plan, tmp_path) is None


def test_legacy_handle_override_refusal_page_names_effective_backend(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A refused overridden harvest pages as the opencode process it really was.

    The row's client is not configured, so the stage position is unresolvable and
    the ``blocked`` sentinel is refused (same shape as
    ``test_local_harvest_opencode_refused_result_pages_with_blocker_and_recovery``).
    The page must not call the dead worker an aider process.
    """
    worktree = make_git_repo("wt-leg-refused")
    reported = make_opencode_blocked(
        ticket_id="T-LEG-REF",
        worktree=worktree,
        reason="merge_conflict_post_push",
        stage_reached="stage2_impl",
    )
    write_opencode_log(worktree, [text_event(framed(reported))])
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    tbt = _save_dead_local_session(
        worktree,
        "T-LEG-REF",
        backend=None,
        session_stage=Stage.FINALIZE,
        row_stage=Stage.FINALIZE,
        row_client="client-unconfigured",
    )

    _harvest_tick(tbt)

    pages = _attention_events("test-leg-refused", "T-LEG-REF")
    assert len(pages) == 1
    assert pages[0]["paused_status"] == "sentinel_stage_mismatch_dead_session"
    breadcrumbs = str(pages[0]["breadcrumbs"])
    assert "dead opencode process" in breadcrumbs
    assert "dead aider process" not in breadcrumbs


def _raise_oserror(**_kwargs: object) -> object:
    msg = "worktree vanished"
    raise OSError(msg)


@pytest.mark.parametrize(("stage", "marker"), _PARK_STAGES)
def test_harvest_exception_fallback_opencode_handle_stages_at_row(
    stage: Stage,
    marker: str,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed opencode harvest stages at the row's marker, never ``stage2_impl``."""
    ticket = f"T-FB-OC-{stage.value}"
    worktree = make_git_repo(f"wt-{ticket}")
    tbt = _save_dead_local_session(
        worktree,
        ticket,
        backend="opencode",
        session_stage=stage,
        row_stage=stage,
    )
    monkeypatch.setattr(
        "cw.reconcile.harvest_synthesis.synthesize_opencode_result", _raise_oserror
    )

    _harvest_tick(tbt)

    result = _last_result(ticket)
    assert result.stage_reached == marker
    assert result.stage_reached != "stage2_impl"
    assert result.scope.lines_actual == (None if marker == "stage1_plan" else 0)
    assert result.next_actions == _OPENCODE_NEXT_ACTIONS
    assert result.blocker is not None
    assert result.blocker.reason == _UNEXPECTED_ERROR
    row = _row(ticket)
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.stage == stage
    warnings = _fallback_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert _stage_mismatch_events(ticket) == []


@pytest.mark.parametrize(("stage", "marker"), _PARK_STAGES)
def test_harvest_exception_fallback_aider_handle_non_repo_worktree_stages_at_row(
    stage: Stage,
    marker: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed git harvest of an advanced row stages at the row's marker too.

    The spawn stage is IMPL (so the backend resolves to ``aider``) while the row
    has advanced; the worktree is not a repo, so real git synthesis raises. No
    monkeypatch: the handler runs for real and exercises the pre-impl scope flip.
    """
    ticket = f"T-FB-AIDER-{stage.value}"
    worktree = tmp_path / "not-a-repo"
    with pytest.raises(subprocess.CalledProcessError):
        run_git(
            ["-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            check=True,
        )
    tbt = _save_dead_local_session(
        worktree,
        ticket,
        backend="aider",
        session_stage=Stage.IMPL,
        row_stage=stage,
    )

    _harvest_tick(tbt)

    result = _last_result(ticket)
    assert result.stage_reached == marker
    assert result.scope.lines_actual == (None if marker == "stage1_plan" else 0)
    assert result.next_actions == _LOCAL_NEXT_ACTIONS
    assert result.blocker is not None
    assert result.blocker.reason == _UNEXPECTED_ERROR
    row = _row(ticket)
    assert row.status == QueueItemStatus.BLOCKED_ON_USER
    assert row.stage == stage
    assert len(_fallback_warnings(caplog)) == 1


def test_harvest_exception_fallback_aider_impl_row_keeps_stage2_impl(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """An IMPL row's fallback result still stages at ``stage2_impl`` (unchanged)."""
    worktree = tmp_path / "not-a-repo"
    tbt = _save_dead_local_session(
        worktree,
        "T-FB-IMPL",
        backend="aider",
        session_stage=Stage.IMPL,
        row_stage=Stage.IMPL,
    )

    _harvest_tick(tbt)

    result = _last_result("T-FB-IMPL")
    assert result.stage_reached == "stage2_impl"
    assert result.scope.lines_actual == 0
    assert result.next_actions == _LOCAL_NEXT_ACTIONS
