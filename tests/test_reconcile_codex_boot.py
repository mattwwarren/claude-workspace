"""Tests for cw.reconcile.codex_boot — boot-time orphaned-codex-session pass (#1727).

Once ``CodexExecutor.spawn()`` hands its review to a background thread, a
crash/SIGKILL can leave an ACTIVE codex session behind with no thread left to
join. This pass, run once before the first dispatch tick, flags exactly those
for operator attention.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import psutil
import pytest

from cw.config import load_clients, load_state, save_state
from cw.dev_queue import add_ticket, load_dev_queue
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
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
    CODEX_ORPHANED_AT_BOOT_DISPOSITION,
    _codex_process_running_in,
    _head_matches_pre_review_ref,
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


def _no_codex_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_boot, "_codex_process_running_in", lambda _wt: False)


def _assert_session_closed() -> None:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at is not None


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


def test_lingering_codex_process_is_parked(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex process still in the worktree could race the requeued attempt."""
    _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    monkeypatch.setattr(codex_boot, "_codex_process_running_in", lambda _wt: True)
    _use_auto_reap_policy(monkeypatch)

    assert reap_orphaned_codex_sessions_at_boot() == 1

    _assert_parked("test-codex-boot-lingering", _PARK_REASON_CODEX_PROCESS_RUNNING)
    _assert_session_closed()


def _task_without_base_ref() -> TicketTask:
    return TicketTask(
        ticket_id="T-orphan",
        client="client-a",
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id="T-orphan",
    )


def _client_a(tmp_config_dir: Path, tmp_path: Path) -> ClientConfig:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    _write_clients_yaml(tmp_config_dir, workspace, "codex")
    return load_clients()["client-a"]


def _no_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fetched: list[str] = []
    monkeypatch.setattr(
        codex_boot,
        "fetch_feature_branch",
        lambda _client, branch: fetched.append(branch),
    )
    return fetched


def test_no_stage_base_ref_falls_back_to_remote_tip_and_requeues_on_match(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo("wt")
    client = _client_a(tmp_config_dir, tmp_path)
    fetched = _no_fetch(monkeypatch)
    git_in(
        repo,
        "update-ref",
        "refs/remotes/origin/main",
        git_in(repo, "rev-parse", "HEAD"),
    )

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), client) is True
    assert fetched == ["main"]


def test_no_stage_base_ref_and_remote_tip_mismatch_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo("wt")
    client = _client_a(tmp_config_dir, tmp_path)
    _no_fetch(monkeypatch)
    stale_sha = git_in(repo, "rev-parse", "HEAD")
    commit_tracked_file(repo, "later.py")
    git_in(repo, "update-ref", "refs/remotes/origin/main", stale_sha)

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), client) is False


def test_no_stage_base_ref_and_unresolvable_remote_parks(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_git_repo("wt")
    client = _client_a(tmp_config_dir, tmp_path)
    _no_fetch(monkeypatch)

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), client) is None


def test_no_stage_base_ref_and_detached_head_is_unknown(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No branch to name means no remote tip to compare against."""
    repo = make_git_repo("wt")
    client = _client_a(tmp_config_dir, tmp_path)
    fetched = _no_fetch(monkeypatch)
    git_in(repo, "checkout", "--detach")

    assert _head_matches_pre_review_ref(repo, _task_without_base_ref(), client) is None
    assert fetched == []


def test_unreadable_head_is_unknown(tmp_config_dir: Path, tmp_path: Path) -> None:
    client = _client_a(tmp_config_dir, tmp_path)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    assert (
        _head_matches_pre_review_ref(not_a_repo, _task_without_base_ref(), client)
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

        monkeypatch.setattr(codex_boot.subprocess, "run", _hang)

        assert _worktree_porcelain_clean_except_verdict(tmp_path) is None


class TestCodexProcessRunningIn:
    """cwd-based scan: the codex child has no persisted PID to pin."""

    @staticmethod
    def _procs(monkeypatch: pytest.MonkeyPatch, *infos: dict[str, object]) -> None:
        monkeypatch.setattr(
            codex_boot.psutil,
            "process_iter",
            lambda _attrs: [SimpleNamespace(info=info) for info in infos],
        )

    def test_codex_in_the_worktree_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(
            monkeypatch,
            {"name": "bash", "cwd": str(tmp_path)},
            {"name": "codex", "cwd": None},
            {"name": "codex", "cwd": str(tmp_path)},
        )

        assert _codex_process_running_in(tmp_path) is True

    def test_codex_elsewhere_does_not_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._procs(monkeypatch, {"name": "codex", "cwd": str(tmp_path / "other")})

        assert _codex_process_running_in(tmp_path) is False

    def test_a_process_that_vanishes_mid_scan_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Vanished:
            @property
            def info(self) -> dict[str, object]:
                raise psutil.NoSuchProcess(pid=1)

        monkeypatch.setattr(
            codex_boot.psutil,
            "process_iter",
            lambda _attrs: [
                _Vanished(),
                SimpleNamespace(info={"name": "codex", "cwd": str(tmp_path)}),
            ],
        )

        assert _codex_process_running_in(tmp_path) is True

    def test_a_failed_process_listing_reads_as_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never raises on the boot path, and an unanswerable scan parks."""

        def _denied(_attrs: object) -> list[object]:
            raise psutil.AccessDenied(pid=1)

        monkeypatch.setattr(codex_boot.psutil, "process_iter", _denied)

        assert _codex_process_running_in(tmp_path) is True

    def test_real_process_table_has_no_codex_in_a_fresh_dir(
        self, tmp_path: Path
    ) -> None:
        assert _codex_process_running_in(tmp_path) is False


def test_missing_worktree_path_is_a_git_error(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    client = _client_a(tmp_config_dir, tmp_path)
    config = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)

    assert _resolve_orphan_action(
        None, _task_without_base_ref(), client, {"client-a": client}, config
    ) == (False, _PARK_REASON_GIT_ERROR)


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
