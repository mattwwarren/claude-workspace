"""Tests for cw.reconcile.codex_boot — boot-time orphaned-codex-session pass (#1727).

Once ``CodexExecutor.spawn()`` hands its review to a background thread, a
crash/SIGKILL can leave an ACTIVE codex session behind with no thread left to
join. This pass, run once before the first dispatch tick, flags exactly those
for operator attention.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import psutil
import pytest

from cw.config import load_clients, load_state, save_state, sessions_lock
from cw.dev_queue import add_ticket, load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import HookContextConflictError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _shared as reconcile_shared
from cw.reconcile import codex_boot
from cw.reconcile.codex_boot import (
    _PARK_REASON_CODEX_PROCESS_RUNNING,
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_NO_WORKTREE_PATH,
    _PARK_REASON_PROCESS_SCAN_INCONCLUSIVE,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    _SCAN_INCONCLUSIVE,
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
    CODEX_ORPHAN_CLOSE_REASON,
    CODEX_ORPHANED_AT_BOOT_DISPOSITION,
    _codex_processes_in,
    _head_matches_pre_review_ref,
    _OrphanDisposition,
    _resolve_orphan_action,
    _worktree_porcelain_clean_except_verdict,
    reap_orphaned_codex_sessions_at_boot,
)
from cw.spawn import _write_hook_context
from tests._reconcile_helpers import _mk_headless_daemon_session
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _write_clients_yaml(
    tmp_config_dir: Path,
    workspace: Path,
    backend: str,
    *,
    names: tuple[str, ...] = ("client-a",),
) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"  {name}:\n"
        f"    workspace_path: {workspace}\n"
        "    default_branch: main\n"
        "    pipeline:\n"
        "      executors:\n"
        "        review:\n"
        f"          backend: {backend}\n"
        for name in names
    )
    (config_dir / "clients.yaml").write_text(f"clients:\n{body}")


def _seed(
    tmp_config_dir: Path,
    tmp_path: Path,
    *,
    backend: str = "codex",
    ticket_id: str = "T-orphan",
    session: Session | None = None,
) -> None:
    """Write clients.yaml, one ACTIVE codex session, and its RUNNING task."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, backend)
    sess = session or _mk_headless_daemon_session(
        ticket_id, tmp_path / "wt", _STARTED_AT
    )
    save_state(CwState(sessions=[sess]))
    add_ticket(
        TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
        )
    )


def _attention_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
    ]


def _requeued_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
    ]


def _use_config(monkeypatch: pytest.MonkeyPatch, **fields: object) -> None:
    """Pin the orchestrator config the boot pass resolves its gates against."""
    config = OrchestratorConfig.model_validate(fields)
    monkeypatch.setattr(codex_boot, "load_effective_config", lambda: config)


def _use_auto_reap_policy(monkeypatch: pytest.MonkeyPatch, **fields: object) -> None:
    """Authorize the requeue branch (gate 0) so a later gate is what decides."""
    _use_config(monkeypatch, reap_policy=ReapPolicy.AUTO, **fields)


def _reap_proposed_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_REAP_PROPOSED],
        )
    ]


def _no_codex_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: [])


def _live_writer(monkeypatch: pytest.MonkeyPatch, *pids: int) -> None:
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: list(pids))


class _FakeCodex:
    """A process-table entry for a codex writer that records any signal.

    Stands in for what ``psutil.process_iter`` yields, so the real cwd scan
    runs against it. Every signalling method records instead of acting: the
    boot pass must never call one.
    """

    def __init__(self, worktree: Path, pid: int = 4242) -> None:
        self.pid = pid
        self.info: dict[str, object] = {"name": "codex", "cwd": str(worktree)}
        self.signals: list[str] = []

    def terminate(self) -> None:
        self.signals.append("SIGTERM")

    def kill(self) -> None:
        self.signals.append("SIGKILL")

    def send_signal(self, sig: int) -> None:
        self.signals.append(str(sig))


def _forbid_os_kill(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record any ``os.kill`` instead of sending it; the pass must send none."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def _completed_events(consumer: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            consumer=consumer,
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
    ]


def _failing_record_event(
    monkeypatch: pytest.MonkeyPatch,
    target: ModuleType,
    failing_type: OrchestratorEventType,
) -> Callable[..., Any]:
    """Make *target*.record_event raise OSError for *failing_type* only.

    Returns the real ``record_event`` so a test can restore it for a retry.
    """
    real: Callable[..., Any] = target.record_event

    def _record(event_type: OrchestratorEventType, *args: Any, **kwargs: Any) -> Any:
        if event_type is failing_type:
            msg = "disk full"
            raise OSError(msg)
        return real(event_type, *args, **kwargs)

    monkeypatch.setattr(target, "record_event", _record)
    return real


def _record_subprocess_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``subprocess.run`` argv, delegating to the real call."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(arg) for arg in args])
        result: subprocess.CompletedProcess[str] = real_run(args, **kwargs)
        return result

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _assert_no_fetch(calls: list[list[str]]) -> None:
    assert any("rev-parse" in argv for argv in calls), "recorder saw no git traffic"
    assert not [argv for argv in calls if "fetch" in argv]


def _assert_session_closed() -> None:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at is not None


def _assert_session_left_active() -> Session:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.completed_at is None
    assert session.completed_reason is None
    return session


def _assert_parked(consumer: str, reason: str) -> None:
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.session_id is None
    payloads = _attention_events(consumer)
    assert len(payloads) == 1
    assert reason in str(payloads[0]["breadcrumbs"])
    assert _requeued_events(f"{consumer}-requeued") == []


def _seed_clean_codex_orphan(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    ticket_id: str = "T-orphan",
) -> tuple[Path, str]:
    """A real git repo, .claude/cw-context.json + review-verdict.md committed
    vs. left dirty per the 'clean except verdict' contract, stage_base_ref
    stamped to HEAD as of the commit *before* review-verdict.md is added.

    Searched for overlapping siblings: none found — closest is
    test_dispatch_branch_freshness.py's _seed_repo, a different domain
    (branch-fetch freshness fixture, not a review-orphan session/task pair).
    """
    repo = make_git_repo("wt")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_clients_yaml(tmp_config_dir, workspace, "codex")
    sess = _mk_headless_daemon_session(ticket_id, repo, _STARTED_AT)
    # Why: intentionally overwrites the session_id key _mk_headless_daemon_session
    # just wrote, to establish a clean committed baseline — a real review-orphan
    # repo's last commit wouldn't carry a stale session id.
    commit_tracked_file(repo, ".claude/cw-context.json", '{"headless": true}')
    head_sha = git_in(repo, "rev-parse", "HEAD")
    save_state(CwState(sessions=[sess]))
    add_ticket(
        TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
            stage_base_ref=head_sha,
        )
    )
    (repo / ".claude" / "review-verdict.md").write_text("verdict text\n")
    return repo, head_sha


def test_orphaned_codex_session_is_flagged(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case this pass exists for: park the task, emit the signal.

    ``auto`` reap policy on purpose: the worktree is not a git repository, so
    this reaches and exercises the git-status gate (``git_error`` park) rather
    than short-circuiting on the reap-policy gate. Either way the orphaned
    session record itself is closed (#2285).
    """
    _use_auto_reap_policy(monkeypatch)
    _seed(tmp_config_dir, tmp_path)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert task.session_id is None

    payloads = _attention_events("test-codex-boot-flagged")
    assert len(payloads) == 1
    assert payloads[0]["paused_status"] == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    assert payloads[0]["ticket_id"] == "T-orphan"
    assert payloads[0]["client"] == "client-a"
    # Breadcrumbs must point the operator at the real risk: a partial commit.
    assert "worktree" in str(payloads[0]["breadcrumbs"])
    assert _PARK_REASON_GIT_ERROR in str(payloads[0]["breadcrumbs"])
    _assert_session_closed()


def test_clean_orphan_with_fix_loop_off_is_requeued(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provably clean + reap_policy auto → back to PENDING for a fresh attempt."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.session_id is None
    assert task.unproductive_attempts == 1
    assert task.disposition is None
    _assert_session_closed()
    assert _attention_events("test-codex-boot-requeue-attention") == []

    payloads = _requeued_events("test-codex-boot-requeued")
    assert len(payloads) == 1
    assert payloads[0]["reason"] == CODEX_ORPHAN_CLEAN_REQUEUE_REASON
    assert payloads[0]["from_stage"] == payloads[0]["to_stage"] == Stage.REVIEW
    assert payloads[0]["ticket_id"] == "T-orphan"
    assert payloads[0]["client"] == "client-a"
    assert "regressed" not in payloads[0]


def _expected_close_audit(
    session: Session, *, disposition: str, detail: str
) -> dict[str, object]:
    return {
        "session_id": session.id,
        "session_name": session.name,
        "client": "client-a",
        "ticket_id": "T-orphan",
        "crashed": True,
        "salvaged": False,
        "reason": CODEX_ORPHAN_CLOSE_REASON,
        "disposition": disposition,
        "detail": detail,
    }


def test_closing_a_requeued_orphan_records_a_completion_audit_event(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    session = load_state().sessions[0]
    assert _completed_events("test-codex-boot-audit-requeued") == [
        _expected_close_audit(
            session,
            disposition="requeued",
            detail=CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
        )
    ]


def test_closing_a_parked_orphan_records_a_completion_audit_event(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    _seed(tmp_config_dir, tmp_path)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    session = load_state().sessions[0]
    assert _completed_events("test-codex-boot-audit-parked") == [
        _expected_close_audit(
            session,
            disposition="parked",
            detail=_PARK_REASON_REAP_POLICY_NOT_AUTO,
        )
    ]


def test_failed_close_audit_leaves_the_session_open_for_a_retry(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit before effect: no event, no close — and no boot-pass crash.

    The task keeps its claim, so the next boot re-finds the orphan and closes
    it with its audit event rather than skipping it on the identity check.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)
    real = _failing_record_event(
        monkeypatch, codex_boot, OrchestratorEventType.SESSION_COMPLETED
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0

    session = _assert_session_left_active()
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == session.id
    assert _requeued_events("test-codex-boot-failed-audit-requeued") == []

    monkeypatch.setattr(codex_boot, "record_event", real)

    assert reap_orphaned_codex_sessions_at_boot() == 1
    _assert_session_closed()
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
    assert len(_completed_events("test-codex-boot-failed-audit-completed")) == 1


def test_one_failing_orphan_does_not_stop_the_pass(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O failure disposing of one orphan is logged; the next still runs."""
    _seed(tmp_config_dir, tmp_path)
    second = _mk_headless_daemon_session("T-second", tmp_path / "wt2", _STARTED_AT)
    state = load_state()
    state.sessions.append(second)
    save_state(state)
    add_ticket(
        TicketTask(
            ticket_id="T-second",
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=second.id,
        )
    )
    real_dispose = codex_boot._close_orphaned_session_and_dispose

    def _dispose(**kwargs: Any) -> bool:
        if kwargs["ticket_id"] == "T-orphan":
            msg = "disk full"
            raise OSError(msg)
        return real_dispose(**kwargs)

    monkeypatch.setattr(codex_boot, "_close_orphaned_session_and_dispose", _dispose)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    assert by_ticket["T-orphan"].status is QueueItemStatus.RUNNING
    assert by_ticket["T-second"].status is QueueItemStatus.BLOCKED_ON_USER


def test_signal_only_still_parks_clean_orphan(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0006: without reap_policy auto, even a clean orphan is parked.

    Every later gate (fix loop, worktree, HEAD, process) would pass here, so
    this proves the reap-policy gate fires ahead of — not instead of — the
    park + session-close machinery.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-signal-only", _PARK_REASON_REAP_POLICY_NOT_AUTO)
    _assert_session_closed()


def test_dirty_worktree_is_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything uncommitted beyond review-verdict.md is not provably clean."""
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    (repo / "extra.txt").write_text("stray\n")
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-dirty", _PARK_REASON_DIRTY_WORKTREE)
    _assert_session_closed()


def test_head_moved_is_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit landed after the review began — possibly a partial fix commit."""
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    commit_tracked_file(repo, "extra.txt")
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-head-moved", _PARK_REASON_HEAD_MOVED)
    _assert_session_closed()


def test_fix_loop_enabled_for_lane_parks_even_when_clean(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the fix loop on, the review may have been mid-fix — never requeue."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch, default_codex_fix_loop_enabled=True)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-fix-loop", _PARK_REASON_FIX_LOOP_ENABLED)
    _assert_session_closed()


@pytest.mark.parametrize("auto", [True, False])
def test_lingering_writer_parks_with_no_signal_sent(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto: bool,
) -> None:
    """A codex process still in the worktree parks the orphan; nothing is killed.

    Under every ``reap_policy``, ``auto`` included: the session stays ACTIVE,
    the reap is proposed for the operator (ADR-0006 signal-only) and the
    breadcrumbs name the pid. The worktree is otherwise clean, so without the
    writer this orphan would have been requeued.
    """
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(repo, pid=4242)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [writer])
    sent = _forbid_os_kill(monkeypatch)
    if auto:
        _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    assert writer.signals == []
    assert sent == []
    consumer = f"test-codex-boot-lingering-{auto}"
    _assert_parked(consumer, "pid 4242")
    breadcrumbs = str(_attention_events(f"{consumer}-2")[0]["breadcrumbs"])
    assert _PARK_REASON_CODEX_PROCESS_RUNNING in breadcrumbs
    session = _assert_session_left_active()
    assert session.reap_proposed_at is not None

    # Emitted by reconcile's shared _emit_reap_proposed, so the payload is
    # that helper's shape (its evidence block, not codex-specific fields).
    proposals = _reap_proposed_events(f"{consumer}-reap")
    assert proposals == [
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": "client-a",
            "ticket_id": "T-orphan",
            "lane": "default",
            "proposed_action": "park_blocked_on_user",
            "reason": ReapReason.CODEX_ORPHAN_LIVE_WRITER.value,
            "evidence": {
                "elapsed_seconds": 0.0,
                "in_roster": False,
                "transcript_age_seconds": None,
                "transcript_mtime_age_seconds": None,
            },
        }
    ]
    # A session left ACTIVE is not closed, so it gets no completion audit.
    assert _completed_events(f"{consumer}-completed") == []


def test_inconclusive_scan_never_counts_as_no_writer(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex candidate whose cwd psutil could not read parks, never requeues.

    ``process_iter`` reports an ``AccessDenied`` attribute as ``None``. The
    scan cannot tell whether that codex sits in the worktree, so an otherwise
    clean orphan under ``auto`` is parked with the session left ACTIVE, not
    requeued on the assumption that no writer is left.
    """
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    unreadable = _FakeCodex(repo, pid=4242)
    unreadable.info["cwd"] = None
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [unreadable])
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-inconclusive", _SCAN_INCONCLUSIVE)
    session = _assert_session_left_active()
    assert session.reap_proposed_at is not None
    assert len(_reap_proposed_events("test-codex-boot-inconclusive-reap")) == 1
    assert unreadable.signals == []


def test_reap_proposal_is_delegated_to_the_shared_emitter(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One SESSION_REAP_PROPOSED emitter: reconcile's, not a local copy."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _live_writer(monkeypatch, 4242)
    calls: list[list[object]] = []

    def _spy(_state: object, candidates: list[object], **_kw: object) -> set[str]:
        calls.append(candidates)
        return set()

    monkeypatch.setattr(codex_boot, "_emit_reap_proposed", _spy)

    reap_orphaned_codex_sessions_at_boot()

    assert len(calls) == 1
    (candidate,) = calls[0]
    assert isinstance(candidate, codex_boot.ReapCandidate)
    assert candidate.proposed_action is codex_boot.ProposedAction.PARK_BLOCKED_ON_USER
    assert candidate.reap_reason is ReapReason.CODEX_ORPHAN_LIVE_WRITER
    assert candidate.ticket_id == "T-orphan"
    assert candidate.client == "client-a"


def test_failed_reap_proposal_leaves_the_stamp_unset_for_a_retry(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emit first, persist the dedup stamp after: a failed emit stamps nothing.

    The task is left claimed too, so the next boot re-finds the orphan and
    proposes again instead of the dedup guard silently swallowing it.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _live_writer(monkeypatch, 4242)
    real = _failing_record_event(
        monkeypatch, reconcile_shared, OrchestratorEventType.SESSION_REAP_PROPOSED
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0

    assert _assert_session_left_active().reap_proposed_at is None
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == load_state().sessions[0].id
    assert _attention_events("test-codex-boot-failed-proposal-attn") == []

    monkeypatch.setattr(reconcile_shared, "record_event", real)

    assert reap_orphaned_codex_sessions_at_boot() == 1
    assert _assert_session_left_active().reap_proposed_at is not None
    assert len(_reap_proposed_events("test-codex-boot-failed-proposal-reap")) == 1


def test_already_proposed_session_is_not_proposed_again(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reap_proposed_at`` is the dedup guard, as in ``_emit_reap_proposed``."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    state = load_state()
    state.sessions[0].reap_proposed_at = _STARTED_AT
    save_state(state)
    _live_writer(monkeypatch, 4242)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-dedup", _PARK_REASON_CODEX_PROCESS_RUNNING)
    assert _assert_session_left_active().reap_proposed_at == _STARTED_AT
    assert _reap_proposed_events("test-codex-boot-dedup-reap") == []


@pytest.mark.parametrize("auto", [True, False])
def test_unscannable_process_table_parks_and_leaves_session_active(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto: bool,
) -> None:
    """No scan means no proof the writer is gone, under either policy."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: None)
    if auto:
        _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked(
        f"test-codex-boot-noscan-{auto}", _PARK_REASON_PROCESS_SCAN_INCONCLUSIVE
    )
    _assert_session_left_active()
    proposals = _reap_proposed_events(f"test-codex-boot-noscan-reap-{auto}")
    assert len(proposals) == 1


def test_skipped_requeue_emits_no_event_and_leaves_the_fresh_claim(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
) -> None:
    """The row belongs to another session by the time the locked revert runs.

    The identity-checked revert skips, so TICKET_REQUEUED must not fire and
    the fresh session's claim must survive untouched.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    store = load_dev_queue()
    store.tasks[0].session_id = "fresh-session"
    save_dev_queue(store)

    codex_boot._requeue_clean_orphan(
        session_id="T-orphan",
        ticket_id="T-orphan",
        client_name="client-a",
        stage=Stage.REVIEW,
    )

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "fresh-session"
    assert task.unproductive_attempts == 0
    assert task.disposition is None
    assert _requeued_events("test-codex-boot-skipped-requeue") == []


def _replace_with_newer_session(state: CwState) -> None:
    """A newer session takes the row over; the orphan's record stays ACTIVE."""
    newer = state.sessions[0].model_copy(
        update={"id": "newer-session", "started_at": datetime.now(UTC)}
    )
    state.sessions.append(newer)
    store = load_dev_queue()
    store.tasks[0].session_id = newer.id
    save_dev_queue(store)


def _resume_in_place(state: CwState) -> None:
    """The same record is resumed onto a new daemon surface (``cw resume``)."""
    state.sessions[0].surface_ref = "resumed-short-id"
    state.sessions[0].resumed_at = datetime.now(UTC)


def _completed_since_snapshot(state: CwState) -> None:
    """Something else already closed the record, with its own reason."""
    state.sessions[0].status = SessionStatus.COMPLETED
    state.sessions[0].completed_at = datetime.now(UTC)
    state.sessions[0].completed_reason = CompletionReason.NORMAL


@pytest.mark.parametrize(
    "supersede",
    [_replace_with_newer_session, _resume_in_place, _completed_since_snapshot],
    ids=["newer-session-owns-the-row", "resumed-in-place", "completed-since"],
)
def test_stale_snapshot_never_overwrites_a_newer_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    supersede: Callable[[CwState], None],
) -> None:
    """A newer session replaces the orphan between the snapshot and the close.

    The close re-checks identity under the lock and finds the snapshot stale,
    so nothing is overwritten: no session is closed as CRASHED, no reap is
    proposed, and the task keeps its claim.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)
    real_resolve = codex_boot._resolve_orphan_action

    superseded: dict[str, object] = {}

    def _resolve_then_superseded(*args: Any) -> _OrphanDisposition:
        disposition = real_resolve(*args)
        state = load_state()
        supersede(state)
        save_state(state)
        superseded["state"] = load_state().model_dump()
        superseded["queue"] = load_dev_queue().model_dump()
        return disposition

    monkeypatch.setattr(codex_boot, "_resolve_orphan_action", _resolve_then_superseded)

    assert reap_orphaned_codex_sessions_at_boot() == 0

    assert load_state().model_dump() == superseded["state"]
    assert load_dev_queue().model_dump() == superseded["queue"]
    consumer = f"test-codex-boot-stale-{supersede.__name__}"
    assert _completed_events(consumer) == []
    assert _reap_proposed_events(f"{consumer}-reap") == []
    assert _requeued_events(f"{consumer}-requeued") == []
    assert _attention_events(f"{consumer}-attn") == []


def test_session_gone_from_state_is_neither_closed_nor_proposed(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A record pruned since the snapshot leaves nothing to close or stamp."""
    _seed(tmp_config_dir, tmp_path)
    before = load_state().model_dump()
    snapshot = load_state().sessions[0].model_copy(update={"id": "no-such-session"})
    disposition = _OrphanDisposition(
        should_requeue=False,
        reason=_PARK_REASON_CODEX_PROCESS_RUNNING,
        close_session=False,
    )

    with sessions_lock():
        acted = codex_boot._close_or_propose_reap(
            snapshot, "T-orphan", "default", disposition
        )

    assert acted is False
    assert load_state().model_dump() == before
    assert _reap_proposed_events("test-codex-boot-gone-reap") == []
    assert _completed_events("test-codex-boot-gone-completed") == []


@pytest.fixture
def hanging_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], None]:
    """Put a ``git`` on PATH that hangs on one subcommand, real git otherwise.

    ``exec sleep`` so the timed-out child IS the sleeper: killing a wrapper
    shell would orphan a ``sleep`` still holding the stdout pipe open.
    """
    real_git = shutil.which("git")
    assert real_git is not None

    def _install(subcommand: str) -> None:
        bin_dir = tmp_path / "hanging-git-bin"
        bin_dir.mkdir()
        script = bin_dir / "git"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "{subcommand}" ]; then exec sleep 60; fi\n'
            f'exec "{real_git}" "$@"\n'
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(codex_boot, "_GIT_SUBPROCESS_TIMEOUT_SECONDS", 0.5)

    return _install


@pytest.mark.parametrize("subcommand", ["status", "rev-parse"])
def test_hanging_git_parks_instead_of_blocking_boot(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    hanging_git: Callable[[str], None],
    subcommand: str,
) -> None:
    """Every git call the reaper makes is bounded; a timeout parks."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)
    hanging_git(subcommand)

    started = time.monotonic()
    assert reap_orphaned_codex_sessions_at_boot() == 1
    assert time.monotonic() - started < 20  # the fake sleeps 60s

    _assert_parked(f"test-codex-boot-hang-{subcommand}", _PARK_REASON_GIT_ERROR)
    _assert_session_closed()


def test_unestablishable_baseline_is_parked_without_fetching(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stage_base_ref and no local origin/<branch> → park; never fetch."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    store = load_dev_queue()
    store.tasks[0].stage_base_ref = None
    save_dev_queue(store)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)
    calls = _record_subprocess_argv(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_no_fetch(calls)
    _assert_parked("test-codex-boot-no-baseline", _PARK_REASON_GIT_ERROR)
    _assert_session_closed()


def _task_without_base_ref() -> TicketTask:
    return TicketTask(
        ticket_id="T-orphan",
        client="client-a",
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id="T-orphan",
    )


def _clients(tmp_config_dir: Path, tmp_path: Path) -> dict[str, ClientConfig]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, "codex")
    return load_clients()


# feature_branch_key("client-a", "T-orphan", ...) with the default "dev" prefix.
_ORIGIN_FEATURE_REF = "refs/remotes/origin/dev/T-orphan"


def test_no_stage_base_ref_falls_back_to_local_tracking_ref_on_match(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo("wt")
    clients = _clients(tmp_config_dir, tmp_path)
    git_in(repo, "update-ref", _ORIGIN_FEATURE_REF, git_in(repo, "rev-parse", "HEAD"))
    calls = _record_subprocess_argv(monkeypatch)

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), clients) is True
    _assert_no_fetch(calls)


def test_no_stage_base_ref_and_tracking_ref_mismatch_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("wt")
    clients = _clients(tmp_config_dir, tmp_path)
    stale_sha = git_in(repo, "rev-parse", "HEAD")
    commit_tracked_file(repo, "later.py")
    git_in(repo, "update-ref", _ORIGIN_FEATURE_REF, stale_sha)

    assert (
        _head_matches_pre_review_ref(repo, _task_without_base_ref(), clients) is False
    )


def test_no_stage_base_ref_and_missing_tracking_ref_is_unknown_without_fetch(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``origin/main`` exists but is not the feature branch's tracking ref."""
    repo = make_git_repo("wt")
    clients = _clients(tmp_config_dir, tmp_path)
    git_in(
        repo,
        "update-ref",
        "refs/remotes/origin/main",
        git_in(repo, "rev-parse", "HEAD"),
    )
    calls = _record_subprocess_argv(monkeypatch)

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), clients) is None
    _assert_no_fetch(calls)


def test_unreadable_head_is_unknown(tmp_config_dir: Path, tmp_path: Path) -> None:
    clients = _clients(tmp_config_dir, tmp_path)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    assert (
        _head_matches_pre_review_ref(not_a_repo, _task_without_base_ref(), clients)
        is None
    )


class TestWorktreePorcelainCleanExceptVerdict:
    """Tri-state: True clean, False dirty, None when git cannot answer."""

    def test_only_the_verdict_is_clean(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("wt")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "review-verdict.md").write_text("v\n")

        assert _worktree_porcelain_clean_except_verdict(repo) is True

    def test_fully_clean_is_clean(self, make_git_repo: Callable[..., Path]) -> None:
        assert _worktree_porcelain_clean_except_verdict(make_git_repo("wt")) is True

    def test_any_other_path_is_dirty(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("wt")
        (repo / "nested").mkdir()
        (repo / "nested" / "stray.py").write_text("x\n")

        assert _worktree_porcelain_clean_except_verdict(repo) is False

    def test_a_rename_onto_the_verdict_path_is_dirty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Rename entries are parsed to their destination AND source: a staged
        rename touches a tracked file, which is never the verdict alone."""
        repo = make_git_repo("wt")
        commit_tracked_file(repo, "notes.md", "v\n")
        (repo / ".claude").mkdir()
        git_in(repo, "mv", "notes.md", ".claude/review-verdict.md")

        assert _worktree_porcelain_clean_except_verdict(repo) is False

    def test_not_a_repository_is_unknown(self, tmp_path: Path) -> None:
        assert _worktree_porcelain_clean_except_verdict(tmp_path) is None

    def test_timeout_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hang(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        monkeypatch.setattr(subprocess, "run", _hang)

        assert _worktree_porcelain_clean_except_verdict(tmp_path) is None


_DELETED = " (deleted)"


class TestCodexProcessesIn:
    """cwd-based scan: the codex child has no persisted PID to pin.

    Fail closed: ``None`` (inconclusive) whenever the scan cannot tell, so an
    unreadable candidate never reads as "no writer".
    """

    @staticmethod
    def _procs(monkeypatch: pytest.MonkeyPatch, *infos: dict[str, object]) -> None:
        processes = [
            SimpleNamespace(info=info, pid=pid) for pid, info in enumerate(infos, 100)
        ]
        monkeypatch.setattr(psutil, "process_iter", lambda _attrs: processes)

    def test_every_codex_in_the_worktree_is_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(
            monkeypatch,
            {"name": "bash", "cwd": str(tmp_path)},
            {"name": "bash", "cwd": None},
            {"name": "codex", "cwd": str(tmp_path)},
            {"name": "codex", "cwd": str(tmp_path)},
        )

        assert _codex_processes_in(tmp_path) == [102, 103]

    @pytest.mark.parametrize(
        "info",
        [{"name": "codex", "cwd": None}, {"name": None, "cwd": "/elsewhere"}],
        ids=["codex-cwd-unreadable", "name-unreadable"],
    )
    def test_an_unreadable_candidate_is_inconclusive(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        info: dict[str, object],
    ) -> None:
        """``process_iter`` fills an ``AccessDenied`` attribute with ``None``.

        A codex whose cwd cannot be read, or a process whose name cannot be,
        may be the writer; a codex found elsewhere does not settle it.
        """
        self._procs(
            monkeypatch, info, {"name": "codex", "cwd": str(tmp_path / "other")}
        )

        assert _codex_processes_in(tmp_path) is None

    def test_codex_elsewhere_does_not_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(monkeypatch, {"name": "codex", "cwd": str(tmp_path / "other")})

        assert _codex_processes_in(tmp_path) == []

    def test_a_cwd_in_a_deleted_worktree_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kernel reports a removed cwd as ``<path> (deleted)``."""
        gone = tmp_path / "wt"
        self._procs(monkeypatch, {"name": "codex", "cwd": f"{gone}{_DELETED}"})

        assert _codex_processes_in(gone) == [100]

    def test_a_deleted_sibling_directory_does_not_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(
            monkeypatch, {"name": "codex", "cwd": f"{tmp_path / 'other'}{_DELETED}"}
        )

        assert _codex_processes_in(tmp_path / "wt") == []

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="the ' (deleted)' cwd form is Linux /proc behaviour",
    )
    def test_a_real_process_in_a_deleted_directory_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the kernel really reports the suffixed form psutil hands us."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(60)",
            ],
            cwd=worktree,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"
            worktree.rmdir()
            name = psutil.Process(proc.pid).name()
            monkeypatch.setattr(codex_boot, "_CODEX_PROCESS_NAME", name)

            found = _codex_processes_in(worktree)

            assert found is not None
            assert proc.pid in found
        finally:
            proc.kill()
            proc.wait()

    def test_a_process_that_vanishes_mid_scan_is_inconclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``NoSuchProcess`` race on any entry leaves the scan unable to tell."""

        class _Vanished:
            @property
            def info(self) -> dict[str, object]:
                raise psutil.NoSuchProcess(pid=1)

        monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [_Vanished()])

        assert _codex_processes_in(tmp_path) is None

    def test_a_failed_process_listing_is_inconclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never raises on the boot path; the caller parks on ``None``."""

        def _denied(_attrs: object) -> list[object]:
            raise psutil.AccessDenied(pid=1)

        monkeypatch.setattr(psutil, "process_iter", _denied)

        assert _codex_processes_in(tmp_path) is None

    def test_real_process_table_has_no_codex_in_a_fresh_dir(
        self, tmp_path: Path
    ) -> None:
        assert _codex_processes_in(tmp_path) == []


def _auto(*, auto: bool) -> OrchestratorConfig:
    return OrchestratorConfig(
        reap_policy=ReapPolicy.AUTO if auto else ReapPolicy.SIGNAL_ONLY
    )


@pytest.mark.parametrize("auto", [True, False])
def test_no_recorded_worktree_path_parks_without_closing(
    tmp_config_dir: Path, tmp_path: Path, *, auto: bool
) -> None:
    """No path means no process scan, so no proof the writer is gone."""
    clients = _clients(tmp_config_dir, tmp_path)

    assert _resolve_orphan_action(
        None, _task_without_base_ref(), clients["client-a"], clients, _auto(auto=auto)
    ) == _OrphanDisposition(
        should_requeue=False,
        reason=_PARK_REASON_NO_WORKTREE_PATH,
        close_session=False,
    )


def test_deleted_worktree_with_a_live_writer_parks_without_closing(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded path is gone, but a codex process still sits in it."""
    clients = _clients(tmp_config_dir, tmp_path)
    gone = tmp_path / "wt"
    process = SimpleNamespace(
        info={"name": "codex", "cwd": f"{gone}{_DELETED}"}, pid=4242
    )
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [process])

    disposition = _resolve_orphan_action(
        gone, _task_without_base_ref(), clients["client-a"], clients, _auto(auto=True)
    )

    assert disposition == _OrphanDisposition(
        should_requeue=False,
        reason=f"{_PARK_REASON_CODEX_PROCESS_RUNNING} (pid 4242)",
        close_session=False,
    )


def test_deleted_worktree_whose_scan_fails_parks_without_closing(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = _clients(tmp_config_dir, tmp_path)

    def _denied(_attrs: object) -> list[object]:
        raise psutil.AccessDenied(pid=1)

    monkeypatch.setattr(psutil, "process_iter", _denied)

    disposition = _resolve_orphan_action(
        tmp_path / "wt",
        _task_without_base_ref(),
        clients["client-a"],
        clients,
        _auto(auto=True),
    )

    assert disposition.reason == _PARK_REASON_PROCESS_SCAN_INCONCLUSIVE
    assert disposition.close_session is False


@pytest.mark.parametrize(
    ("auto", "reason"),
    [(True, _PARK_REASON_GIT_ERROR), (False, _PARK_REASON_REAP_POLICY_NOT_AUTO)],
)
def test_deleted_worktree_with_no_writer_closes_as_before(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto: bool,
    reason: str,
) -> None:
    """Scan found nothing: the usual gates decide, and the session closes."""
    clients = _clients(tmp_config_dir, tmp_path)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [])

    assert _resolve_orphan_action(
        tmp_path / "wt",
        _task_without_base_ref(),
        clients["client-a"],
        clients,
        _auto(auto=auto),
    ) == _OrphanDisposition(should_requeue=False, reason=reason)


def test_non_codex_backend_session_is_left_alone(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A claude-native REVIEW session at boot is not this pass's business."""
    _seed(tmp_config_dir, tmp_path, backend="claude-native")

    assert reap_orphaned_codex_sessions_at_boot() == 0

    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
    assert _attention_events("test-codex-boot-non-codex") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", SessionStatus.COMPLETED),
        ("origin", SessionOrigin.USER),
    ],
)
def test_ineligible_session_shape_is_skipped(
    tmp_config_dir: Path, tmp_path: Path, field: str, value: object
) -> None:
    """Only live DAEMON sessions are eligible — mirrors the stalled sweep's gate."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    setattr(sess, field, value)
    _seed(tmp_config_dir, tmp_path, session=sess)

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_non_headless_session_is_skipped(tmp_config_dir: Path, tmp_path: Path) -> None:
    """An interactive session's worktree is not a headless orphan."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    _seed(tmp_config_dir, tmp_path, session=sess)
    (tmp_path / "wt" / ".claude" / "cw-context.json").write_text('{"headless": false}')

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_session_without_a_matching_task_is_skipped_without_raising(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """No dev-queue row to park → skip quietly; never raise on a boot path."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, "codex")
    save_state(
        CwState(
            sessions=[
                _mk_headless_daemon_session("T-nope", tmp_path / "wt", _STARTED_AT)
            ]
        )
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0


def test_session_with_unparseable_name_is_skipped(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A session name carrying no auto-dev ticket id yields no ticket to park."""
    sess = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    sess.name = "client-a/interactive-impl"
    _seed(tmp_config_dir, tmp_path, session=sess)

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_same_ticket_id_on_two_clients_does_not_collide(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Ticket numbering is per-client, so the queue lookup must key on both.

    Two clients each own a ticket numbered 21. Only client-b's is the codex
    REVIEW orphan; client-a's row is at PLAN and is added last, so a lookup
    keyed on ticket_id alone resolves to it and reads a non-codex backend —
    silently skipping the real orphan and leaving the wrong client's row in
    the match. Keying on (ticket_id, client) is what makes this pass.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(
        tmp_config_dir, workspace, "codex", names=("client-a", "client-b")
    )

    orphan = _mk_headless_daemon_session("21", tmp_path / "wt-b", _STARTED_AT)
    orphan.client = "client-b"
    orphan.name = "client-b/auto-dev/21"
    save_state(CwState(sessions=[orphan]))

    add_ticket(
        TicketTask(
            ticket_id="21",
            client="client-b",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id=orphan.id,
        )
    )
    add_ticket(
        TicketTask(
            ticket_id="21",
            client="client-a",
            stage=Stage.PLAN,
            status=QueueItemStatus.RUNNING,
            session_id="live-a",
        )
    )

    assert reap_orphaned_codex_sessions_at_boot() == 1

    by_client = {t.client: t for t in load_dev_queue().tasks}
    assert by_client["client-b"].status is QueueItemStatus.BLOCKED_ON_USER
    assert by_client["client-b"].disposition == CODEX_ORPHANED_AT_BOOT_DISPOSITION
    # client-a's same-numbered ticket is a different task and stays untouched.
    assert by_client["client-a"].status is QueueItemStatus.RUNNING
    assert by_client["client-a"].session_id == "live-a"

    payloads = _attention_events("test-codex-boot-collision")
    assert len(payloads) == 1
    assert payloads[0]["client"] == "client-b"


def test_zombie_session_does_not_park_a_newer_sessions_task(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A stale ACTIVE record must not park the row a *later* session now owns.

    An earlier boot's crash orphan can linger in state as an ACTIVE Session
    long after its task was parked, recovered, and re-dispatched onto a fresh,
    healthy session. Matching on (ticket_id, client) alone re-finds that zombie
    on every subsequent boot and parks the live review as a false-positive
    orphan. Only the row whose recorded session_id *is* the orphaned session is
    this pass's business.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, "codex")

    zombie = _mk_headless_daemon_session("T-orphan", tmp_path / "wt", _STARTED_AT)
    save_state(CwState(sessions=[zombie]))
    add_ticket(
        TicketTask(
            ticket_id="T-orphan",
            client="client-a",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
            session_id="live-successor",
        )
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "live-successor"
    assert task.disposition is None
    assert _attention_events("test-codex-boot-zombie") == []
    # The identity check skips the zombie before any close/dispose logic runs.
    assert load_state().sessions[0].status is SessionStatus.ACTIVE


def test_closing_orphaned_session_clears_hook_context_conflict_guard(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A parked orphan must not block the next DAEMON spawn into its worktree.

    Before #2285 the orphaned Session stayed ACTIVE forever, so a later
    ``_write_hook_context`` into the same worktree read the stale
    ``cw-context.json``, found a non-terminal session behind it, and raised.
    """
    _seed(tmp_config_dir, tmp_path)
    worktree = tmp_path / "wt"

    assert reap_orphaned_codex_sessions_at_boot() == 1

    (worktree / ".claude" / "cw-context.json").write_text(
        json.dumps({"headless": True, "session_id": "T-orphan"})
    )
    _write_hook_context(
        worktree,
        session_id="new-sess",
        session_name="client-a/auto-dev/T-orphan",
        client="client-a",
        purpose="impl",
        ticket_id="T-orphan",
        origin=SessionOrigin.DAEMON,
    )

    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    assert context["session_id"] == "new-sess"


def test_hook_context_guard_still_fires_for_a_live_session(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Control for the test above: without the reaper, the guard raises."""
    _seed(tmp_config_dir, tmp_path)

    with pytest.raises(HookContextConflictError):
        _write_hook_context(
            tmp_path / "wt",
            session_id="new-sess",
            session_name="client-a/auto-dev/T-orphan",
            client="client-a",
            purpose="impl",
            ticket_id="T-orphan",
            origin=SessionOrigin.DAEMON,
        )


def test_unknown_client_is_skipped(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A session whose client is no longer declared cannot resolve a backend."""
    _seed(tmp_config_dir, tmp_path)
    config_dir = tmp_config_dir / ".config" / "cw"
    (config_dir / "clients.yaml").write_text(
        "clients:\n  other:\n    workspace_path: /tmp\n"
    )

    assert reap_orphaned_codex_sessions_at_boot() == 0
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
