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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import psutil
import pytest

from cw.config import load_clients, load_state, save_state
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
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import codex_boot
from cw.reconcile.codex_boot import (
    _PARK_REASON_CODEX_PROCESS_RUNNING,
    _PARK_REASON_CODEX_PROCESS_SURVIVED,
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_PROCESS_SCAN_FAILED,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
    CODEX_ORPHAN_LIVE_WRITER_REAP_REASON,
    CODEX_ORPHANED_AT_BOOT_DISPOSITION,
    _codex_processes_in,
    _head_matches_pre_review_ref,
    _OrphanDisposition,
    _resolve_orphan_action,
    _terminate_codex_processes,
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


class _FakeCodex:
    """Stands in for the ``psutil.Process`` the cwd scan matched.

    *dies_on* is the signal after which ``wait`` succeeds: ``"SIGTERM"``,
    ``"SIGKILL"``, or ``None`` for a process that outlives both.
    """

    def __init__(self, pid: int = 4242, *, dies_on: str | None = "SIGTERM") -> None:
        self.pid = pid
        self.signals: list[str] = []
        self._dies_on = dies_on

    def terminate(self) -> None:
        self.signals.append("SIGTERM")

    def kill(self) -> None:
        self.signals.append("SIGKILL")

    def wait(self, timeout: float | None = None) -> int:
        if self._dies_on is None or self._dies_on not in self.signals:
            raise psutil.TimeoutExpired(timeout or 0, pid=self.pid)
        return 0


def _live_writer(monkeypatch: pytest.MonkeyPatch, *processes: object) -> None:
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: list(processes))


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


def test_live_writer_under_auto_that_dies_is_closed_and_disposed_normally(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto: terminate the lingering writer first, then the usual gates decide.

    The worktree is otherwise clean, so once the writer is gone the orphan
    requeues and its session closes.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(dies_on="SIGTERM")
    _live_writer(monkeypatch, writer)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    assert writer.signals == ["SIGTERM"]
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
    assert len(_requeued_events("test-codex-boot-writer-dies")) == 1
    _assert_session_closed()
    assert _reap_proposed_events("test-codex-boot-writer-dies-reap") == []


def test_live_writer_under_auto_escalates_to_sigkill(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(dies_on="SIGKILL")
    _live_writer(monkeypatch, writer)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    assert writer.signals == ["SIGTERM", "SIGKILL"]
    _assert_session_closed()


def test_live_writer_under_auto_that_will_not_die_leaves_session_active(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that survives SIGKILL is still writing: park, never close."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(pid=4242, dies_on=None)
    _live_writer(monkeypatch, writer)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    assert writer.signals == ["SIGTERM", "SIGKILL"]
    _assert_parked("test-codex-boot-undying", _PARK_REASON_CODEX_PROCESS_SURVIVED)
    breadcrumbs = str(_attention_events("test-codex-boot-undying-2")[0]["breadcrumbs"])
    assert "pid 4242" in breadcrumbs
    session = _assert_session_left_active()
    assert session.reap_proposed_at is None
    assert _reap_proposed_events("test-codex-boot-undying-reap") == []


def test_live_writer_under_signal_only_is_not_killed_and_reap_is_proposed(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0006 signal_only: no kill, no close — park and propose the reap."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    writer = _FakeCodex(pid=4242)
    _live_writer(monkeypatch, writer)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    assert writer.signals == []
    _assert_parked("test-codex-boot-signal-only-writer", "pid 4242")
    breadcrumbs = str(
        _attention_events("test-codex-boot-signal-only-writer-2")[0]["breadcrumbs"]
    )
    assert _PARK_REASON_CODEX_PROCESS_RUNNING in breadcrumbs
    session = _assert_session_left_active()
    assert session.reap_proposed_at is not None

    proposals = _reap_proposed_events("test-codex-boot-signal-only-reap")
    assert len(proposals) == 1
    assert proposals[0] == {
        "session_id": session.id,
        "session_name": session.name,
        "client": "client-a",
        "ticket_id": "T-orphan",
        "lane": "default",
        "proposed_action": "park_blocked_on_user",
        "reason": CODEX_ORPHAN_LIVE_WRITER_REAP_REASON,
        "evidence": {"codex_pids": [4242], "worktree": str(session.worktree_path)},
    }


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
    _live_writer(monkeypatch, _FakeCodex())

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

    _assert_parked(f"test-codex-boot-noscan-{auto}", _PARK_REASON_PROCESS_SCAN_FAILED)
    _assert_session_left_active()
    proposals = _reap_proposed_events(f"test-codex-boot-noscan-reap-{auto}")
    assert len(proposals) == (0 if auto else 1)


def test_skipped_requeue_emits_no_event_and_leaves_the_fresh_claim(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is re-claimed between the snapshot and the locked revert.

    The identity-checked revert skips, so TICKET_REQUEUED must not fire and
    the fresh session's claim must survive untouched.
    """
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _no_codex_process(monkeypatch)
    _use_auto_reap_policy(monkeypatch)
    real_resolve = codex_boot._resolve_orphan_action

    def _resolve_then_reclaimed(*args: Any) -> _OrphanDisposition:
        disposition = real_resolve(*args)
        store = load_dev_queue()
        store.tasks[0].session_id = "fresh-session"
        save_dev_queue(store)
        return disposition

    monkeypatch.setattr(codex_boot, "_resolve_orphan_action", _resolve_then_reclaimed)

    reap_orphaned_codex_sessions_at_boot()

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "fresh-session"
    assert task.unproductive_attempts == 0
    assert task.disposition is None
    assert _requeued_events("test-codex-boot-skipped-requeue") == []
    assert _attention_events("test-codex-boot-skipped-requeue-attn") == []


def test_session_gone_from_state_is_neither_closed_nor_proposed(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A record pruned since the snapshot leaves nothing to close or stamp."""
    _seed(tmp_config_dir, tmp_path)
    before = load_state().model_dump()
    disposition = _OrphanDisposition(
        should_requeue=False,
        reason=_PARK_REASON_CODEX_PROCESS_RUNNING,
        close_session=False,
        propose_reap=True,
        live_writer_pids=(4242,),
    )

    assert (
        codex_boot._close_or_propose_reap(
            "no-such-session", "T-orphan", "default", disposition
        )
        is None
    )
    assert load_state().model_dump() == before


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


class TestCodexProcessesIn:
    """cwd-based scan: the codex child has no persisted PID to pin."""

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
            {"name": "codex", "cwd": None},
            {"name": "codex", "cwd": str(tmp_path)},
            {"name": "codex", "cwd": str(tmp_path)},
        )

        found = _codex_processes_in(tmp_path)

        assert found is not None
        assert [p.pid for p in found] == [102, 103]

    def test_codex_elsewhere_does_not_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(monkeypatch, {"name": "codex", "cwd": str(tmp_path / "other")})

        assert _codex_processes_in(tmp_path) == []

    def test_a_process_that_vanishes_mid_scan_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Vanished:
            @property
            def info(self) -> dict[str, object]:
                raise psutil.NoSuchProcess(pid=1)

        survivor = SimpleNamespace(
            info={"name": "codex", "cwd": str(tmp_path)}, pid=200
        )
        monkeypatch.setattr(
            psutil, "process_iter", lambda _attrs: [_Vanished(), survivor]
        )

        found = _codex_processes_in(tmp_path)

        assert found is not None
        assert [p.pid for p in found] == [200]

    def test_a_failed_process_listing_is_unknown(
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


class TestTerminateCodexProcesses:
    """SIGTERM, bounded wait, SIGKILL, bounded wait; returns surviving pids."""

    @staticmethod
    def _spawn(code: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        return proc

    def test_a_real_process_dies_on_sigterm(self) -> None:
        proc = self._spawn("import time; print('ready', flush=True); time.sleep(60)")

        assert _terminate_codex_processes([psutil.Process(proc.pid)]) == []
        assert not psutil.pid_exists(proc.pid)

    def test_a_real_sigterm_ignoring_process_is_sigkilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(codex_boot, "_CODEX_TERMINATE_WAIT_SECONDS", 0.5)
        proc = self._spawn(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            " print('ready', flush=True); time.sleep(60)"
        )

        assert _terminate_codex_processes([psutil.Process(proc.pid)]) == []
        assert not psutil.pid_exists(proc.pid)

    def test_a_process_that_outlives_sigkill_is_reported(self) -> None:
        writer = _FakeCodex(pid=4242, dies_on=None)

        assert _terminate_codex_processes([writer]) == [4242]
        assert writer.signals == ["SIGTERM", "SIGKILL"]

    def test_a_process_already_gone_counts_as_terminated(self) -> None:
        class _Gone(_FakeCodex):
            def terminate(self) -> None:
                raise psutil.NoSuchProcess(pid=self.pid)

        assert _terminate_codex_processes([_Gone()]) == []

    def test_a_process_we_may_not_signal_is_reported(self) -> None:
        class _Denied(_FakeCodex):
            def terminate(self) -> None:
                raise psutil.AccessDenied(pid=self.pid)

        assert _terminate_codex_processes([_Denied(pid=7)]) == [7]


def test_missing_worktree_path_is_a_git_error(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    clients = _clients(tmp_config_dir, tmp_path)
    config = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)

    assert _resolve_orphan_action(
        None, _task_without_base_ref(), clients["client-a"], clients, config
    ) == _OrphanDisposition(should_requeue=False, reason=_PARK_REASON_GIT_ERROR)


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
