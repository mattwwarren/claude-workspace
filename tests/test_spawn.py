"""Tests for cw spawn and spawn close commands."""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args

import pytest
from click.testing import CliRunner

from cw._lock_guard import held_locks
from cw.auto_dev_result import AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION, Status
from cw.cli import main
from cw.codex_review import CODEX_REVIEW_UNPARSEABLE
from cw.config import load_state, orchestrator_config_file, save_state
from cw.dev_queue import load_dev_queue
from cw.exceptions import CwError, SpawnUnregisteredError, WorkerLaunchedError
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    MUST_FIX_OVERRIDE_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    ClientConfig,
    CompletionReason,
    CwState,
    MustFixOverride,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.liveness_page import close_command
from cw.spawn import (
    _stop_hook_command,
    build_disallowed_tools_arg,
    emit_spawn_post_launch_attention,
    spawn_create_impl,
)
from cw.worktree import live_home_reason
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _mk_routed_session,
    _write_agent_spawn_stamp,
)
from tests.conftest import (
    _SRC_ROOT,
    _make_daemon_session,
    _make_ticket_task,
    _seed_completed_session,
    _seed_daemon_session,
    _stop_leaves_worker_listed,
    _stop_makes_roster_unreadable,
    post_launch_attention_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.native_daemon import NativeDaemonClient
    from tests.conftest import CapturedEvent

# The post-launch page's breadcrumb keeps this many characters of the error
# before appending "…" (#2502), the same cap as dispatch.tick's last_error.
_BREADCRUMB_DETAIL_MAX = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Stand-in absolute context path for tests that inspect the generated
# settings.local.json content without going through a real worktree.
_FAKE_CONTEXT_PATH = Path("/wt/.claude/cw-context.json")


def _make_client(tmp_path: Path, name: str = "test-client") -> ClientConfig:
    """Create a ClientConfig pointing at a tmp workspace directory."""
    workspace = tmp_path / "workspace" / name
    workspace.mkdir(parents=True)
    return ClientConfig(
        name=name,
        workspace_path=workspace,
        default_branch="main",
    )


def _make_prompt_file(tmp_path: Path, content: str = "Do the thing.") -> Path:
    """Write a prompt file and return its path."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(content)
    return prompt_file


def _write_test_client_yaml(tmp_config_dir: Path, tmp_path: Path) -> None:
    """Write a minimal clients.yaml for 'test-client' (mirrors
    test_dev_queue.py's ``_write_client_yaml``) so ``requeue_ticket``'s
    ``get_client`` lookup resolves during ``--requeue`` CLI tests."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "requeue-ws"
    ws.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  test-client:\n    workspace_path: {ws}\n"
    )


def _seed_running_task(
    ticket_id: str = "GEN-42",
    client: str = "test-client",
    session_id: str = "test1234",
) -> TicketTask:
    """Create and save a RUNNING TicketTask in the dev queue."""
    from cw.dev_queue import save_dev_queue
    from cw.models import DevQueueStore, QueueItemStatus

    task = _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
    )
    store = DevQueueStore(tasks=[task])
    save_dev_queue(store)
    return task


class _SpawnStopProbeDaemon(LockProbeDaemon):
    """``LockProbeDaemon`` plus the dev-queue snapshot and a raise-once knob (#2547).

    Each stop also appends ``{ticket_id: status}`` for every queued task to
    :attr:`queue_statuses`. With ``raise_first_stop`` the first stop raises
    ``OSError`` after the base fake has recorded the call and the probe.
    """

    def __init__(self, *, raise_first_stop: bool = False) -> None:
        super().__init__()
        self.queue_statuses: list[dict[str, QueueItemStatus]] = []
        self.raise_first_stop = raise_first_stop

    def stop(self, short_id: str) -> None:
        self.queue_statuses.append(
            {t.ticket_id: t.status for t in load_dev_queue().tasks}
        )
        super().stop(short_id)
        if self.raise_first_stop and len(self.stop_calls) == 1:
            msg = "daemon socket gone"
            raise OSError(msg)


# ---------------------------------------------------------------------------
# Unit-level tests (no Click runner, fake daemon client injected directly)
# ---------------------------------------------------------------------------


class TestSpawnCreate:
    """Tests for the spawn_create business logic."""

    def test_happy_path_creates_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn create: happy path stores session with correct fields."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path, "Implement the feature.")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-happy")

        session_id = _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            label="my-task",
            native_daemon=daemon,
        )

        # Session persisted
        state = load_state()
        assert len(state.sessions) == 1
        sess = state.sessions[0]
        assert sess.id == session_id
        assert sess.name == "test-client/my-task"
        assert sess.client == "test-client"
        assert sess.purpose == SessionPurpose.IMPL
        assert sess.origin == SessionOrigin.DAEMON
        assert sess.worktree_path == worktree
        assert sess.workspace_path == client.workspace_path
        assert sess.surface_ref is not None
        assert sess.status == SessionStatus.ACTIVE

    def test_default_label_is_daemon(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn create: default label produces 'client/daemon' session name."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-default-label")

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            label=None,
            native_daemon=daemon,
        )

        state = load_state()
        assert state.sessions[0].name == "test-client/daemon"

    def test_daemon_receives_cwd_and_prompt(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_bg gets the worktree path and the raw prompt verbatim.

        Regression guard: the old tmux path inlined env vars and a ``cd``
        prefix into a shell command string. The native path passes cwd
        separately and the prompt unmodified — no shell wrapping, no
        indirection through a wrapper command.
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="acme")
        prompt = "Fix the login bug."
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-cwd-prompt")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt=prompt,
            label=None,
            native_daemon=daemon,
        )

        assert len(daemon.spawn_calls) == 1
        cwd_arg, prompt_arg = daemon.spawn_calls[0]
        assert cwd_arg == worktree
        assert prompt_arg == prompt

    def test_surface_ref_stores_native_short_id(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """surface_ref carries the short Claude session id returned by spawn_bg."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-surface-ref")

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            label=None,
            native_daemon=daemon,
        )

        state = load_state()
        # FakeNativeDaemonClient yields "00000001" for first spawn call.
        assert state.sessions[0].surface_ref == "00000001"

    def test_parent_linkage_writes_both_directions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn create with parent: worker.parent_session_id set and parent's
        worker_session_ids list contains the new worker id (single state save).
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-parent-linkage")

        # Seed a parent orchestrator session in state.
        parent_workspace = tmp_path / "workspace" / "orch"
        parent_workspace.mkdir(parents=True)
        parent = Session(
            name="orch/impl",
            client="orch",
            purpose=SessionPurpose.IMPL,
            workspace_path=parent_workspace,
        )
        state = load_state()
        state.sessions.append(parent)
        save_state(state)

        worker_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-9 --headless",
            label="auto-dev-GEN-9",
            native_daemon=daemon,
            parent=parent.id,
        )

        state = load_state()
        worker = state.find_by_name_or_id(worker_id)
        assert worker is not None
        assert worker.parent_session_id == parent.id
        refreshed_parent = state.find_by_name_or_id(parent.id)
        assert refreshed_parent is not None
        assert worker_id in refreshed_parent.worker_session_ids

    def test_parent_not_found_raises(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn create with bogus parent ID: CwError, no session created, no spawn."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-parent-not-found")

        with pytest.raises(CwError, match="Parent session not found"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev GEN-9 --headless",
                label=None,
                native_daemon=daemon,
                parent="does-not-exist",
            )

        # No worker session persisted, no spawn called.
        state = load_state()
        assert state.sessions == []
        assert daemon.spawn_calls == []

    def test_parent_resolves_by_claude_session_id(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """A parent= value that is a claude_session_id (not the cw id) resolves.

        Regression for #2149: find_by_name_or_id only checked (name, id);
        find_session_by_id also checks claude_session_id.
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-parent-claude-id")

        parent_workspace = tmp_path / "workspace" / "orch"
        parent_workspace.mkdir(parents=True)
        claude_id = "0f2901e1-bb58-4a2c-9c1e-abcdef012345"
        parent = Session(
            name="orch/impl",
            client="orch",
            purpose=SessionPurpose.IMPL,
            workspace_path=parent_workspace,
            claude_session_id=claude_id,
        )
        state = load_state()
        state.sessions.append(parent)
        save_state(state)

        worker_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-9 --headless",
            label="auto-dev-GEN-9",
            native_daemon=daemon,
            parent=claude_id,
        )

        state = load_state()
        worker = state.find_by_name_or_id(worker_id)
        assert worker is not None
        # Resolved to the parent's cw id, not the claude id passed in.
        assert worker.parent_session_id == parent.id
        assert worker.parent_session_id != claude_id
        refreshed_parent = state.find_by_name_or_id(parent.id)
        assert refreshed_parent is not None
        assert worker_id in refreshed_parent.worker_session_ids

    def test_parent_resolves_from_archive(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """A parent= value present only in an archived sessions.<date>.json resolves.

        Regression for #2149: find_by_name_or_id never scans archives at all;
        find_session_by_id does. The archived parent is not in state.sessions,
        so the reverse-link worker_session_ids mutation must be safely skipped
        rather than crashing.
        """
        from datetime import timedelta

        from freezegun import freeze_time

        from cw.session_retention import _SESSION_RETENTION_DAYS, prune_sessions
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-parent-archive")

        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        old = now - timedelta(days=_SESSION_RETENTION_DAYS + 10)
        parent_workspace = tmp_path / "workspace" / "orch"
        parent_workspace.mkdir(parents=True)
        parent = Session(
            id="archpar1",
            name="orch/impl",
            client="orch",
            purpose=SessionPurpose.IMPL,
            workspace_path=parent_workspace,
            status=SessionStatus.COMPLETED,
            started_at=old,
            completed_at=old,
        )
        state = load_state()
        state.sessions.append(parent)
        save_state(state)

        with freeze_time(now):
            result = prune_sessions()
        assert result.archived_count == 1
        # Confirm the parent is genuinely gone from the hot file.
        assert load_state().sessions == []

        worker_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-9 --headless",
            label="auto-dev-GEN-9",
            native_daemon=daemon,
            parent="archpar1",
        )

        state = load_state()
        worker = state.find_by_name_or_id(worker_id)
        assert worker is not None
        assert worker.parent_session_id == "archpar1"
        # The archived parent never re-enters the hot file, and no crash
        # occurred trying to append to its (non-hot) worker_session_ids.
        assert [s.id for s in state.sessions] == [worker_id]

    def test_spawn_create_impl_stamps_lane(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_create_impl(lane='x') stamps session.lane == 'x'."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-lane-stamp")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-42 --headless",
            label="auto-dev-GEN-42",
            native_daemon=daemon,
            lane="test-lane",
        )

        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        assert sess is not None
        assert sess.lane == "test-lane"

    def test_spawn_create_impl_default_lane_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_create_impl with no lane kwarg leaves session.lane as None."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-lane-default")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-43 --headless",
            label="auto-dev-GEN-43",
            native_daemon=daemon,
        )

        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        assert sess is not None
        assert sess.lane is None


class TestSpawnCreateImplWorkerModel:
    """Tests for ClientConfig.worker_model forwarding through spawn_create_impl
    to ``claude --bg`` via ``extra_args`` (issue #248).
    """

    def test_spawn_create_impl_with_worker_model_pins_model(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """When worker_model is set, spawn_bg gets --model <id> as extra_args."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "acme"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="acme",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-sonnet-4-6-20251015",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-worker-model")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--model",
            "claude-sonnet-4-6-20251015",
        ]

    def test_spawn_create_impl_no_worker_model_omits_model_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """When worker_model is unset, extra_args is None (no --model flag)."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-no-worker-model")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] is None

    def test_spawn_create_impl_worker_model_haiku_passes_through_opaque(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """worker_model is opaque — any string is threaded verbatim."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "thrifty"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="thrifty",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-haiku-4-5-20251001",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-haiku-pinned")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--model",
            "claude-haiku-4-5-20251001",
        ]


class TestSpawnCreateImplExtraArgsPermissionMode:
    """Tests for extra_args and permission_mode on spawn_create_impl (issue #294)."""

    def test_spawn_create_impl_passes_extra_args(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Caller-provided extra_args reach spawn_bg."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-extra-args")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
            extra_args=["--resume", "abc12345"],
        )

        assert daemon.spawn_extra_args[0] == ["--resume", "abc12345"]

    def test_spawn_create_impl_merges_worker_model_and_extra_args(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """worker_model args come first, then caller extra_args."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "merged"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="merged",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-sonnet-4-6-20251015",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-merged-args")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
            extra_args=["--resume", "abc12345"],
        )

        assert daemon.spawn_extra_args[0] == [
            "--model",
            "claude-sonnet-4-6-20251015",
            "--resume",
            "abc12345",
        ]

    def test_spawn_create_impl_passes_permission_mode(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Non-None permission_mode propagates to spawn_bg."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-permission-mode")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
            permission_mode="bypassPermissions",
        )

        assert daemon.spawn_permission_modes[0] == "bypassPermissions"

    def test_spawn_create_impl_permission_mode_default_is_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """permission_mode defaults to None (spawn_bg uses _DEFAULT_PERMISSION_MODE)."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-permission-default")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_permission_modes[0] is None


class TestSpawnCreateImplPermissionModeFromModel:
    """Non-auto-capable worker_model pins derive bypassPermissions (#1111)."""

    def test_non_auto_model_derives_bypass_permissions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Haiku pin + no explicit permission_mode → bypassPermissions."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "haiku-derive"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="haiku-derive",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-haiku-4-5-20251001",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-haiku-derive")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_permission_modes[0] == "bypassPermissions"

    def test_auto_capable_model_stays_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Auto-capable pin + no explicit permission_mode → None (default auto)."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "sonnet-derive"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="sonnet-derive",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-sonnet-4-6-20251015",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-sonnet-derive")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
        )

        assert daemon.spawn_permission_modes[0] is None

    def test_explicit_permission_mode_overrides_non_auto_model(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Explicit caller permission_mode wins over model-derived fallback."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "haiku-explicit"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="haiku-explicit",
            workspace_path=workspace,
            default_branch="main",
            worker_model="claude-haiku-4-5-20251001",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-haiku-explicit")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Do the thing.",
            label=None,
            native_daemon=daemon,
            permission_mode="acceptEdits",
        )

        assert daemon.spawn_permission_modes[0] == "acceptEdits"


class TestValidateWorktree:
    """Tests for the _validate_worktree pre-flight gate (issue #186).

    Catches the bug where 'git worktree add -b <branch>' fails (branch already
    exists) but the directory was already mkdir'd by the shell, leaving cw
    spawn to run on an empty dir without complaint.
    """

    def test_spawn_create_impl_rejects_nonexistent_worktree(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Nonexistent path raises WorktreeError; no daemon call or state write."""
        from cw.exceptions import WorktreeError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = tmp_path / "does-not-exist"
        # Deliberately NOT mkdir'd.

        with pytest.raises(WorktreeError, match="does not exist"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 186 --headless",
                label=None,
                native_daemon=daemon,
            )

        state = load_state()
        assert state.sessions == []
        assert daemon.spawn_calls == []

    def test_spawn_create_impl_rejects_worktree_without_git_dir(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Bare directory (no .git/): WorktreeError raised, no side effects.

        Regression for the exact #186 symptom: shell mkdir'd the path but
        'git worktree add' failed, leaving an empty dir.
        """
        from cw.exceptions import WorktreeError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = tmp_path / "empty"
        worktree.mkdir()  # Plain dir, no .git/.

        with pytest.raises(WorktreeError, match="not a git checkout"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 186 --headless",
                label=None,
                native_daemon=daemon,
            )

        state = load_state()
        assert state.sessions == []
        assert daemon.spawn_calls == []
        # cw-context.json must NOT have been written either.
        assert not (worktree / ".claude").exists()

    def test_spawn_create_impl_rejects_worktree_where_rev_parse_fails(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """`.git` exists as a stray file (not a real worktree marker): WorktreeError.

        Belt-and-suspenders: `.git` can be a file (worktree gitdir pointer) or
        symlink — existence alone is insufficient. `git rev-parse --git-dir`
        is the ground truth that git itself accepts the path.
        """
        from cw.exceptions import WorktreeError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = tmp_path / "corrupt"
        worktree.mkdir()
        # `.git` as a file with garbage — passes the existence check but
        # `git rev-parse --git-dir` will reject it.
        (worktree / ".git").write_text("garbage not a gitdir pointer")

        with pytest.raises(WorktreeError, match="rev-parse"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 186 --headless",
                label=None,
                native_daemon=daemon,
            )

        state = load_state()
        assert state.sessions == []
        assert daemon.spawn_calls == []

    def test_spawn_create_impl_accepts_valid_worktree(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Happy path: real git repo passes validation, session is created."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("valid-worktree")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 186 --headless",
            label="valid",
            native_daemon=daemon,
        )

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].id == session_id

    def test_dispatch_path_rejects_invalid_worktree(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Regression for the dispatch.py:153 call site (issue #186 decision #3).

        Simulates create_worktree returning an unvalidated path (as it does
        today — it does no post-validation). The validation gate in
        spawn_create_impl catches it before any daemon spawn.
        """
        from cw.exceptions import WorktreeError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        # Simulate the #186 symptom: create_worktree's git worktree add failed
        # but the dir got mkdir'd anyway by the shell.
        bad_worktree = tmp_path / "wt" / "auto-dev-186"
        bad_worktree.mkdir(parents=True)

        with pytest.raises(WorktreeError, match="not a git checkout"):
            spawn_create_impl(
                client=client,
                worktree=bad_worktree,
                prompt="/auto-dev 186 --headless",
                label="auto-dev/186",
                native_daemon=daemon,
                ticket_id="186",
                headless=True,
            )

        state = load_state()
        assert state.sessions == []
        assert daemon.spawn_calls == []


def _hook_entries(
    built: dict[str, dict[str, list[object]]], event: str
) -> list[dict[str, object]]:
    """Narrow one ``_build_hook_settings()["hooks"][event]`` list to dicts.

    ``_build_hook_settings``'s return type is ``dict[str, dict[str,
    list[object]]]`` -- accurate for what it builds, a heterogeneous
    settings.local.json blob -- but every entry this test class reads back
    out of it is in fact a dict. Narrow once here instead of an
    ``entry["..."]`` cast at every call site below.
    """
    return [cast("dict[str, object]", entry) for entry in built["hooks"][event]]


def _entry_hooks(entry: dict[str, object]) -> list[dict[str, object]]:
    """Narrow one hook entry's own ``"hooks"`` list the same way."""
    return [
        cast("dict[str, object]", hook) for hook in cast("list[object]", entry["hooks"])
    ]


class TestHookSettingsTemplate:
    """The settings.local.json template wires both hooks (#940 R5 + #147)."""

    def test_template_has_pretooluse_guard_and_preserves_stop(self) -> None:
        """PreToolUse/Bash/cw guard-cwd is present; Stop/cw signal-stop preserved."""
        from cw.spawn import _build_hook_settings

        built = _build_hook_settings(_FAKE_CONTEXT_PATH)

        stop_entries = _hook_entries(built, "Stop")
        assert any(
            _entry_hooks(entry)[0]["command"] == _stop_hook_command(_FAKE_CONTEXT_PATH)
            for entry in stop_entries
        )

        pretooluse_entries = _hook_entries(built, "PreToolUse")
        assert any(
            entry.get("matcher") == "Bash"
            and _entry_hooks(entry)[0]["command"] == "cw guard-cwd"
            for entry in pretooluse_entries
        )

    def test_template_includes_busy_wait_guard_pretooluse(self) -> None:
        """#1946: guard-busy-wait rides the SAME Bash entry as guard-cwd.

        Encodes the shape directly: exactly one "Bash"-matched PreToolUse
        entry exists, and both commands sit in that one entry's hooks list.
        A second top-level "Bash" entry would be a different (unverified)
        dispatch question about how Claude Code handles duplicate matchers.
        """
        from cw.spawn import _build_hook_settings

        entries = _hook_entries(_build_hook_settings(_FAKE_CONTEXT_PATH), "PreToolUse")
        bash_entries = [e for e in entries if e.get("matcher") == "Bash"]
        assert len(bash_entries) == 1

        commands = [hook["command"] for hook in _entry_hooks(bash_entries[0])]
        assert commands == [
            "cw guard-cwd",
            "cw guard-busy-wait",
            "cw background-tool-guard-pre",
        ]

    def test_pretooluse_commands_stay_unguarded_literals(self) -> None:
        """#2226 guarded the Stop hook ONLY — PreToolUse commands are untouched.

        The PreToolUse commands still pay an interpreter start per
        invocation; wrapping them is a separate (unfiled) question. Pinning
        the bare literals here makes any future guard an explicit decision
        rather than a copy-paste side effect.
        """
        from cw.spawn import _build_hook_settings

        entries = _hook_entries(_build_hook_settings(_FAKE_CONTEXT_PATH), "PreToolUse")
        commands = [
            hook["command"] for entry in entries for hook in _entry_hooks(entry)
        ]
        assert commands == [
            "cw guard-cwd",
            "cw guard-busy-wait",
            "cw background-tool-guard-pre",
            "cw agent-spawn-pre",
            "cw background-tool-guard-pre",
        ]

    def test_hook_settings_template_includes_agent_spawn_pretooluse(self) -> None:
        """#1646: a subagent-tool PreToolUse entry sits alongside the Bash guard."""
        from cw.spawn import _AGENT_TOOL_MATCHER, _build_hook_settings

        entries = _hook_entries(_build_hook_settings(_FAKE_CONTEXT_PATH), "PreToolUse")
        assert any(
            entry.get("matcher") == _AGENT_TOOL_MATCHER
            and _entry_hooks(entry)[0]["command"] == "cw agent-spawn-pre"
            for entry in entries
        )
        # Must not regress the pre-existing Bash guard entry.
        assert any(entry.get("matcher") == "Bash" for entry in entries)

    def test_hook_settings_template_includes_monitor_pretooluse(self) -> None:
        """#2303: a Monitor-matched entry runs the background-tool guard.

        Written into the same worktree-level PreToolUse block as the Bash and
        Agent/Task entries, which is the settings surface every headless
        worker (and its subagents, which share the worktree cwd) loads.
        """
        from cw.spawn import _AGENT_TOOL_MATCHER, _build_hook_settings

        entries = _hook_entries(_build_hook_settings(_FAKE_CONTEXT_PATH), "PreToolUse")
        monitor_entries = [e for e in entries if e.get("matcher") == "Monitor"]
        assert len(monitor_entries) == 1
        assert _entry_hooks(monitor_entries[0]) == [
            {"type": "command", "command": "cw background-tool-guard-pre"}
        ]
        # Alongside, not replacing, the pre-existing entries.
        assert any(entry.get("matcher") == "Bash" for entry in entries)
        assert any(entry.get("matcher") == _AGENT_TOOL_MATCHER for entry in entries)

    def test_hook_settings_template_has_no_posttooluse_agent_spawn_entry(self) -> None:
        """#1947: the PostToolUse:Agent decrement wiring is removed.

        Replaying a live async ``Agent(isolation="worktree")`` spawn
        (session ea2f3d42/#1902) confirmed it fired at launch-return, not
        subagent completion -- the counter balanced to 0 while the harness's
        own turn accounting (``pendingBackgroundAgentCount``) still showed
        the subagent pending. ``cw signal-stop`` now owns the write instead
        (``tests/test_cli_stop_hook.py``). No ``PostToolUse`` key should
        exist in the template at all -- it was the only entry in it.
        """
        from cw.spawn import _build_hook_settings

        assert "PostToolUse" not in _build_hook_settings(_FAKE_CONTEXT_PATH)["hooks"]

    def test_agent_tool_matcher_is_anchored_and_matches_captured_tool_name(
        self,
    ) -> None:
        """The matcher matches the empirically-captured tool name, and only it.

        Both facts were captured live (2026-08-12) against Claude Code with a
        temporary catch-all hook in a dispatch worktree: a subagent spawn
        reports ``tool_name: "Agent"`` (NOT ``"Task"``, which the ticket prose
        assumed), and an anchored alternation matcher fires for it while
        leaving ``Bash`` alone. The anchor is load-bearing — unanchored
        ``Task`` would also match unrelated tool names such as ``TaskStop``.
        """
        import re

        from cw.spawn import _AGENT_TOOL_MATCHER

        pattern = re.compile(_AGENT_TOOL_MATCHER)
        assert pattern.search("Agent")
        # Legacy/alternate name kept for version robustness.
        assert pattern.search("Task")
        assert not pattern.search("Bash")
        assert not pattern.search("TaskStop")
        assert not pattern.search("AgentOutputStyle")


class TestHookContextInjection:
    """Tests for the Stop-hook + cw-context file injection (issue #147)."""

    def test_writes_settings_and_context_files(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_create_impl writes .claude/settings.local.json + cw-context.json."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-hook-ctx")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 137 --headless",
            label="auto-dev/137",
            native_daemon=daemon,
            ticket_id="137",
        )

        settings_path = worktree / ".claude" / "settings.local.json"
        context_path = worktree / ".claude" / "cw-context.json"
        assert settings_path.exists()
        assert context_path.exists()

        settings = json.loads(settings_path.read_text())
        stop_hooks = settings["hooks"]["Stop"]
        assert any(
            entry["hooks"][0]["command"] == _stop_hook_command(context_path.resolve())
            for entry in stop_hooks
        )

        context = json.loads(context_path.read_text())
        assert context["session_id"] == session_id
        assert context["session_name"] == "test-client/auto-dev/137"
        assert context["client"] == "test-client"
        assert context["purpose"] == "impl"
        assert context["ticket_id"] == "137"
        # #402: the worker's isolation anchor — its own resolved worktree path.
        assert context["worktree_path"] == str(worktree.resolve())

    def test_ticket_id_optional_writes_null(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """When no ticket_id is supplied, cw-context.json carries null."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-ticket-null")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="just do it",
            label=None,
            native_daemon=daemon,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["ticket_id"] is None

    def test_cli_headless_flag_writes_headless_true_to_context(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """`cw spawn --headless` plumbs `headless: true` into cw-context.json.

        Without this, the signal_stop Layer 1 backstop (issue #176) won't
        activate for sessions spawned directly via the CLI — only dev-queue
        dispatch sets the flag today. Manual meta-test fan-out (parallel
        /auto-dev runs on the same ticket via cw spawn) needs the same
        backstop coverage that dev-queue dispatch gets.
        """
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path, "/auto-dev 171 --headless")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-headless-true")

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            label="meta-171-a",
            headless=True,
            native_daemon=daemon,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["headless"] is True

    def test_cli_headless_flag_defaults_to_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """`cw spawn` (no --headless) leaves `headless: false` in context.

        Back-compat: existing callers (not /auto-dev dispatch) don't get the
        backstop applied to them.
        """
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path, "some prompt")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-headless-false")

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            label=None,
            native_daemon=daemon,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["headless"] is False


class TestWriteHookContext:
    """Tests for _write_hook_context's origin-aware settings.local.json behavior.

    Phase B of multiplexer-removal (issue #165): the function must keep its
    existing blind-overwrite behavior for DAEMON-origin (fresh cw-owned
    worktree) but refuse to clobber an existing settings.local.json in a
    USER-origin worktree (the user owns that file).
    """

    def _call(
        self,
        worktree: Path,
        *,
        origin: SessionOrigin,
        session_id: str = "sess-write-hook",
        session_name: str = "test-client/auto-dev/137",
        client: str = "test-client",
        purpose: str = "impl",
        ticket_id: str | None = "137",
    ) -> None:
        from cw.spawn import _write_hook_context

        _write_hook_context(
            worktree,
            session_id=session_id,
            session_name=session_name,
            client=client,
            purpose=purpose,
            ticket_id=ticket_id,
            origin=origin,
        )

    def test_write_hook_context_daemon_origin_clobbers(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON-origin: pre-existing settings.local.json gets overwritten.

        The worktree was freshly created by cw, so any content there is from
        a prior (now defunct) cw spawn — safe to clobber with the current
        hook template.
        """
        worktree = tmp_path / "worktree"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        settings_path = claude_dir / "settings.local.json"
        prior = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"x": "y"}]}]}}
        settings_path.write_text(json.dumps(prior))

        self._call(worktree, origin=SessionOrigin.DAEMON)

        rewritten = json.loads(settings_path.read_text())
        stop_hooks = rewritten["hooks"]["Stop"]
        assert any(
            entry["hooks"][0]["command"]
            == _stop_hook_command((claude_dir / "cw-context.json").resolve())
            for entry in stop_hooks
        )
        # The guard bakes in the absolute context path of THIS worktree (#2226):
        # no env var, no relative path, no cwd dependence.
        command = stop_hooks[0]["hooks"][0]["command"]
        assert str((worktree / HOOK_CONTEXT_RELATIVE_PATH).resolve()) in command
        assert "$" not in command
        # Prior unrelated content is gone — confirms blind overwrite.
        assert rewritten != prior

    def test_write_hook_context_user_origin_raises_on_existing_settings(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """USER-origin: existing settings.local.json → HookContextConflictError."""
        from cw.exceptions import HookContextConflictError

        worktree = tmp_path / "worktree"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        settings_path = claude_dir / "settings.local.json"
        prior_text = json.dumps({"permissions": {"allow": ["Bash(ls)"]}})
        settings_path.write_text(prior_text)

        with pytest.raises(HookContextConflictError):
            self._call(worktree, origin=SessionOrigin.USER)

        # File untouched.
        assert settings_path.read_text() == prior_text

    def test_write_hook_context_user_origin_writes_when_no_settings(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """USER-origin + no existing file → writes hook template successfully."""
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        self._call(worktree, origin=SessionOrigin.USER)

        settings_path = worktree / ".claude" / "settings.local.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        context_path = worktree / ".claude" / "cw-context.json"
        stop_hooks = settings["hooks"]["Stop"]
        assert any(
            entry["hooks"][0]["command"] == _stop_hook_command(context_path.resolve())
            for entry in stop_hooks
        )
        command = stop_hooks[0]["hooks"][0]["command"]
        assert str(context_path.resolve()) in command
        assert "$" not in command
        # Correlation file should still be written.
        assert context_path.exists()

    def test_write_hook_context_skips_settings_local_json_when_write_stop_hook_false(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2280: write_stop_hook=False skips settings.local.json entirely.

        ``CodexExecutor.spawn()`` never involves a Claude session, so there is
        no Stop hook to install — only cw-context.json's correlation metadata
        (including ``prior_attempts_summary``) is wanted on that path.
        """
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        _write_hook_context(
            worktree,
            session_id="sess-codex",
            session_name="test-client/auto-dev/137",
            client="test-client",
            purpose="impl",
            ticket_id="137",
            origin=SessionOrigin.DAEMON,
            write_stop_hook=False,
        )

        assert not (worktree / ".claude" / "settings.local.json").exists()
        assert (worktree / HOOK_CONTEXT_RELATIVE_PATH).exists()

    def test_write_hook_context_default_write_stop_hook_true_is_byte_identical(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2280: adding write_stop_hook must not change existing callers.

        Same worktree, same params, called twice — once with the parameter
        omitted (existing callers' shape) and once with it passed explicitly
        as ``True`` — must produce byte-identical settings.local.json.
        """
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        settings_path = worktree / ".claude" / "settings.local.json"

        _write_hook_context(
            worktree,
            session_id="sess-a",
            session_name="test-client/auto-dev/137",
            client="test-client",
            purpose="impl",
            ticket_id="137",
            origin=SessionOrigin.DAEMON,
        )
        without_param = settings_path.read_text()

        _write_hook_context(
            worktree,
            session_id="sess-a",
            session_name="test-client/auto-dev/137",
            client="test-client",
            purpose="impl",
            ticket_id="137",
            origin=SessionOrigin.DAEMON,
            write_stop_hook=True,
        )
        with_param = settings_path.read_text()

        assert without_param == with_param

    def test_write_hook_context_prior_attempts_summary_with_write_stop_hook_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#2280: the write_stop_hook=False path still collects prior_attempts_summary.

        A prior terminal codex-review park (COMPLETED, blocker.reason=
        codex_review_unparseable) is a TERMINAL_SESSION_STATUSES member, so
        ``_collect_prior_attempts_summary`` picks it up the same way it would
        for a Claude-native attempt — this is the CodexExecutor call shape.
        """
        worktree = make_git_repo("wt-codex-prior-attempts")
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-C",
            client="test-client",
            status=SessionStatus.COMPLETED,
            last_result={
                "status": "blocked",
                "stage_reached": "stage3_review",
                "blocker": {
                    "stage": "stage3_review",
                    "reason": CODEX_REVIEW_UNPARSEABLE,
                    "details": "reviewer (codex_timeout)",
                },
            },
        )
        task = _make_pending_task(ticket_id="838-C", attempts=1)

        # _call doesn't thread task through — reach _write_hook_context
        # directly so world_state_snapshot.prior_attempts_summary is built.
        from cw.spawn import _write_hook_context

        _write_hook_context(
            worktree,
            session_id="sess-codex-2",
            session_name="test-client/auto-dev/838-C",
            client="test-client",
            purpose="impl",
            ticket_id="838-C",
            origin=SessionOrigin.DAEMON,
            task=task,
            write_stop_hook=False,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summary = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summary) == 1
        assert summary[0]["blocker_reason"] == CODEX_REVIEW_UNPARSEABLE


class TestWriteHookContextAtomicAndLiveSession:
    """Tests for issue #427 fixes: atomic writes + DAEMON live-session guard.

    Covers:
    - Both hook files are written via atomic_write_text (no O_TRUNC window).
    - DAEMON overwrite when cw-context.json references a LIVE session → raises.
    - DAEMON overwrite when cw-context.json references a non-live session → ok.
    - DAEMON overwrite when cw-context.json is absent → ok.
    """

    def _call(
        self,
        worktree: Path,
        *,
        origin: SessionOrigin,
        session_id: str = "sess-atomic-427",
        session_name: str = "test-client/auto-dev/427",
        client: str = "test-client",
        purpose: str = "impl",
        ticket_id: str | None = "427",
        daemon: NativeDaemonClient | None = None,
    ) -> None:
        from cw.spawn import _write_hook_context

        _write_hook_context(
            worktree,
            session_id=session_id,
            session_name=session_name,
            client=client,
            purpose=purpose,
            ticket_id=ticket_id,
            origin=origin,
            daemon=daemon,
        )

    def test_settings_written_via_atomic_write(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings.local.json is written through atomic_write_text, not write_text.

        Verifies that atomic_write_text is called for the settings file so a
        concurrent reader never observes an empty/partial file (no O_TRUNC window).
        """
        import cw.spawn as spawn_mod
        from cw.atomic import atomic_write_text as real_atomic

        calls: list[tuple[Path, str]] = []

        def tracking_atomic(path: Path, text: str) -> None:
            calls.append((path, text))
            real_atomic(path, text)

        monkeypatch.setattr(spawn_mod, "atomic_write_text", tracking_atomic)

        worktree = tmp_path / "worktree-atomic-settings"
        worktree.mkdir(parents=True)

        self._call(worktree, origin=SessionOrigin.DAEMON)

        settings_path = worktree / ".claude" / "settings.local.json"
        assert any(p == settings_path for p, _ in calls), (
            "settings.local.json must be written via atomic_write_text"
        )

    def test_context_written_via_atomic_write(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cw-context.json is written through atomic_write_text, not write_text.

        Verifies that the concurrent-reader (Stop hook reads cw-context.json
        every turn) never observes an empty/partial file.
        """
        import cw.spawn as spawn_mod
        from cw.atomic import atomic_write_text as real_atomic

        calls: list[tuple[Path, str]] = []

        def tracking_atomic(path: Path, text: str) -> None:
            calls.append((path, text))
            real_atomic(path, text)

        monkeypatch.setattr(spawn_mod, "atomic_write_text", tracking_atomic)

        worktree = tmp_path / "worktree-atomic-context"
        worktree.mkdir(parents=True)

        self._call(worktree, origin=SessionOrigin.DAEMON)

        context_path = worktree / ".claude" / "cw-context.json"
        assert any(p == context_path for p, _ in calls), (
            "cw-context.json must be written via atomic_write_text"
        )

    def test_daemon_overwrite_proceeds_when_existing_context_is_corrupt(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin: an unparseable existing cw-context.json is ignored.

        A corrupt/partial cw-context.json (e.g. left by a crash mid-write)
        must not block reuse: the read raises JSONDecodeError, the prior
        session id stays None, and the overwrite proceeds normally.
        """
        worktree = tmp_path / "worktree-corrupt-context"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        context_path = claude_dir / "cw-context.json"
        context_path.write_text("{ this is not valid json")

        # Must not raise despite the corrupt prior context.
        self._call(worktree, origin=SessionOrigin.DAEMON)

        # The corrupt content was replaced with a well-formed context.
        rewritten = json.loads(context_path.read_text())
        assert rewritten["session_id"] == "sess-atomic-427"

    def test_daemon_overwrite_raises_when_context_references_live_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin: existing cw-context.json with a LIVE session_id → raises.

        When create_worktree returns an EXISTING worktree (idempotent path), the
        prior session's hook state must NOT be silently overwritten if that session
        is still live in cw state.
        """
        from cw.config import save_state
        from cw.exceptions import HookContextConflictError
        from cw.models import (
            CwState,
            Session,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
        )

        # Seed a live (ACTIVE) session in state.
        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        live_sess = Session(
            id="live1234",
            name="test-client/auto-dev/LIVE-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
        )
        save_state(CwState(sessions=[live_sess]))

        # Pre-write a cw-context.json that references the live session.
        worktree = tmp_path / "worktree-live-guard"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        prior_context = {
            "session_id": "live1234",
            "session_name": "test-client/auto-dev/LIVE-1",
            "client": "test-client",
            "purpose": "impl",
            "ticket_id": "LIVE-1",
            "headless": False,
        }
        (claude_dir / "cw-context.json").write_text(json.dumps(prior_context))

        with pytest.raises(HookContextConflictError, match="live"):
            self._call(worktree, origin=SessionOrigin.DAEMON)

        # cw-context.json must NOT have been overwritten.
        remaining = json.loads((claude_dir / "cw-context.json").read_text())
        assert remaining["session_id"] == "live1234"

    def test_daemon_overwrite_raises_carries_conflicting_session_id(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """GitHub #1674: the raised error names the session that blocks reuse.

        Same fixture shape as the message-matching test above, but asserts on
        the typed evidence the dispatch claim path stamps onto the task so
        concierge recipe 1 can refuse a futile requeue against this exact
        session.
        """
        from cw.config import save_state
        from cw.exceptions import HookContextConflictError
        from cw.models import (
            CwState,
            Session,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
        )

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        live_sess = Session(
            id="live1234",
            name="test-client/auto-dev/LIVE-1",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
        )
        save_state(CwState(sessions=[live_sess]))

        worktree = tmp_path / "worktree-live-guard-id"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        prior_context = {
            "session_id": "live1234",
            "session_name": "test-client/auto-dev/LIVE-1",
            "client": "test-client",
            "purpose": "impl",
            "ticket_id": "LIVE-1",
            "headless": False,
        }
        (claude_dir / "cw-context.json").write_text(json.dumps(prior_context))

        with pytest.raises(HookContextConflictError) as excinfo:
            self._call(worktree, origin=SessionOrigin.DAEMON)

        assert excinfo.value.conflicting_session_id == "live1234"

    def _seed_live_prior_context(
        self, tmp_path: Path, *, homed_on_worktree: bool
    ) -> Path:
        """Seed an ACTIVE session referenced by the worktree's cw-context.json.

        *homed_on_worktree* sets the session's ``worktree_path`` to the target
        worktree, which is what lets ``live_home_reason`` corroborate liveness
        independently of the bare non-terminal status (#2077).
        """
        from cw.models import (
            CwState,
            Session,
            SessionPurpose,
            SessionStatus,
        )

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        worktree = tmp_path / "worktree-genuinely-live"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        live_sess = Session(
            id="live2077",
            name="test-client/auto-dev/LIVE-2077",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
            worktree_path=worktree if homed_on_worktree else None,
        )
        save_state(CwState(sessions=[live_sess]))
        prior_context = {
            "session_id": "live2077",
            "session_name": "test-client/auto-dev/LIVE-2077",
            "client": "test-client",
            "purpose": "impl",
            "ticket_id": "LIVE-2077",
            "headless": False,
        }
        (claude_dir / "cw-context.json").write_text(json.dumps(prior_context))
        return worktree

    def test_daemon_overwrite_raises_genuinely_live_message_when_daemon_confirms(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2077: a prior session that ``live_home_reason`` independently
        confirms is homed on the worktree raises with ``genuinely_live`` set and
        a message that tells the operator NOT to close it."""
        from cw.exceptions import HookContextConflictError

        worktree = self._seed_live_prior_context(tmp_path, homed_on_worktree=True)

        with pytest.raises(HookContextConflictError) as excinfo:
            self._call(
                worktree, origin=SessionOrigin.DAEMON, daemon=FakeNativeDaemonClient()
            )

        assert excinfo.value.genuinely_live is True
        assert excinfo.value.conflicting_session_id == "live2077"
        assert "genuinely live" in str(excinfo.value)
        assert "Do not close it" in str(excinfo.value)
        assert "Complete or close that session" not in str(excinfo.value)

    def test_daemon_overwrite_raises_old_message_when_daemon_cannot_confirm_liveness(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2077: non-terminal in cw state but NOT corroborated by
        ``live_home_reason`` (no session homed on the worktree, empty roster)
        keeps the pre-#2077 message and ``genuinely_live=False``."""
        from cw.exceptions import HookContextConflictError

        worktree = self._seed_live_prior_context(tmp_path, homed_on_worktree=False)

        with pytest.raises(HookContextConflictError) as excinfo:
            self._call(
                worktree, origin=SessionOrigin.DAEMON, daemon=FakeNativeDaemonClient()
            )

        assert excinfo.value.genuinely_live is False
        assert excinfo.value.conflicting_session_id == "live2077"
        assert "Complete or close that session before reusing this worktree." in str(
            excinfo.value
        )

    def test_daemon_overwrite_without_daemon_keeps_old_message(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2077: no *daemon* passed -- liveness is never probed, so even a
        session homed on the worktree keeps the pre-#2077 message."""
        from cw.exceptions import HookContextConflictError

        worktree = self._seed_live_prior_context(tmp_path, homed_on_worktree=True)

        with pytest.raises(HookContextConflictError) as excinfo:
            self._call(worktree, origin=SessionOrigin.DAEMON)

        assert excinfo.value.genuinely_live is False
        assert "Complete or close that session" in str(excinfo.value)

    def test_daemon_overwrite_allowed_when_context_references_completed_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin: existing cw-context.json with a COMPLETED session_id → ok.

        The prior session is done; overwriting its hook state is safe.
        """
        from cw.config import save_state
        from cw.models import (
            CwState,
            Session,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
        )

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        dead_sess = Session(
            id="dead5678",
            name="test-client/auto-dev/DEAD-2",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.COMPLETED,
            workspace_path=workspace,
        )
        save_state(CwState(sessions=[dead_sess]))

        worktree = tmp_path / "worktree-dead-ok"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        prior_context = {
            "session_id": "dead5678",
            "session_name": "test-client/auto-dev/DEAD-2",
            "client": "test-client",
            "purpose": "impl",
            "ticket_id": "DEAD-2",
            "headless": False,
        }
        (claude_dir / "cw-context.json").write_text(json.dumps(prior_context))

        # Should NOT raise — COMPLETED session means safe to overwrite.
        self._call(worktree, origin=SessionOrigin.DAEMON, session_id="new-sess-id")

        # cw-context.json updated with new session id.
        updated = json.loads((claude_dir / "cw-context.json").read_text())
        assert updated["session_id"] == "new-sess-id"

    def test_daemon_overwrite_allowed_when_no_context_file(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin: no prior cw-context.json → overwrite proceeds as before."""
        worktree = tmp_path / "worktree-no-ctx"
        worktree.mkdir(parents=True)

        # No pre-existing cw-context.json — must succeed.
        self._call(worktree, origin=SessionOrigin.DAEMON, session_id="brand-new")

        context_path = worktree / ".claude" / "cw-context.json"
        assert context_path.exists()
        ctx = json.loads(context_path.read_text())
        assert ctx["session_id"] == "brand-new"

    def test_daemon_overwrite_allowed_when_context_session_not_in_state(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin: cw-context.json references unknown session_id → ok.

        The session may have been pruned from state; treating it as non-live is
        correct — safe to overwrite.
        """
        # State is empty (no sessions saved).
        worktree = tmp_path / "worktree-unknown-sess"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": "ghost-id",
                    "session_name": "x",
                    "client": "x",
                    "purpose": "impl",
                    "ticket_id": None,
                    "headless": False,
                }
            )
        )

        # Must not raise — ghost-id is not in state.
        self._call(worktree, origin=SessionOrigin.DAEMON, session_id="replacement")

        ctx = json.loads((claude_dir / "cw-context.json").read_text())
        assert ctx["session_id"] == "replacement"

    def test_daemon_overwrite_raises_for_idle_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON origin with IDLE (non-terminal) session in context raises."""
        from cw.config import save_state
        from cw.exceptions import HookContextConflictError
        from cw.models import (
            CwState,
            Session,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
        )

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        idle_sess = Session(
            id="idle9999",
            name="test-client/auto-dev/IDLE-3",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.IDLE,
            workspace_path=workspace,
        )
        save_state(CwState(sessions=[idle_sess]))

        worktree = tmp_path / "worktree-idle-guard"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": "idle9999",
                    "session_name": "test-client/auto-dev/IDLE-3",
                    "client": "test-client",
                    "purpose": "impl",
                    "ticket_id": "IDLE-3",
                    "headless": False,
                }
            )
        )

        with pytest.raises(HookContextConflictError, match="live"):
            self._call(worktree, origin=SessionOrigin.DAEMON)


class TestSpawnClose:
    """Tests for the spawn close business logic."""

    def _seed_daemon_session(self, tmp_path: Path, tmp_config_dir: Path) -> Session:
        """Save a DAEMON session to state and return it."""
        return _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="dead1234",
            name="test-client/my-task",
            surface_ref="abc12345",
        )

    def test_happy_path_marks_completed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: session marked COMPLETED after close."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        state = load_state()
        closed = state.find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.completed_at is not None

    def test_daemon_close_routes_through_native_daemon(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON-origin sessions are stopped via the native daemon client."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        assert daemon.stop_calls == ["abc12345"]

    def test_user_origin_legacy_surface_ref_skipped(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """USER-origin sessions with legacy surface_ref are logged and skipped."""
        from cw.cli import _spawn_close_impl

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        sess = Session(
            id="user0001",
            name="test-client/impl",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.USER,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
            surface_ref="tmux-pane-7",
        )
        save_state(CwState(sessions=[sess]))
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id="user0001", native_daemon=daemon)

        # No native daemon stop (not a DAEMON session)
        assert daemon.stop_calls == []
        # Session still marked COMPLETED
        state = load_state()
        closed = state.find_by_name_or_id("user0001")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED

    def test_missing_session_raises_cw_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: raises CwError when session_id not found."""
        from cw.cli import _spawn_close_impl

        daemon = FakeNativeDaemonClient()
        error_msg = ""
        try:
            _spawn_close_impl(session_id="nonexistent", native_daemon=daemon)
        except CwError as exc:
            error_msg = str(exc)
        else:
            pytest.fail("Expected CwError was not raised")

        assert "nonexistent" in error_msg

    def test_already_completed_stops_surface_and_exits_clean(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2480: an already-completed session's daemon surface is stopped
        instead of erroring -- a stray roster worker from a completion path
        that never called daemon.stop() must clear on a repeat close, not
        just fail loudly forever."""
        from cw.cli import _spawn_close_impl

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        sess = Session(
            id="done1234",
            name="test-client/my-task",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.COMPLETED,
            workspace_path=workspace,
            surface_ref="deadbeef",
        )
        save_state(CwState(sessions=[sess]))
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id="done1234", native_daemon=daemon)

        assert daemon.stop_calls == ["deadbeef"]
        state = load_state()
        closed = state.find_by_name_or_id("done1234")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED

    def test_no_surface_ref_skips_backend_close(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: neither backend is invoked when surface_ref is None."""
        from cw.cli import _spawn_close_impl

        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        sess = Session(
            id="nosurf1",
            name="test-client/my-task",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
            surface_ref=None,
        )
        save_state(CwState(sessions=[sess]))
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id="nosurf1", native_daemon=daemon)

        assert daemon.stop_calls == []
        state = load_state()
        closed = state.find_by_name_or_id("nosurf1")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED

    # -- #2547: the daemon stop runs after sessions_lock is released --------

    def test_live_daemon_stop_runs_after_sessions_lock_released(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The stop is a <=10s subprocess: it must not hold ``sessions_lock``,
        and the COMPLETED stamp is already persisted when it runs."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        daemon = LockProbeDaemon()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        assert daemon.probes == [("free", {sess.id: SessionStatus.COMPLETED})]
        assert daemon.stop_calls == ["abc12345"]
        closed = load_state().find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.completed_reason == CompletionReason.USER

    def test_already_completed_stop_runs_after_sessions_lock_released(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2480's repeat-close stop also runs with ``sessions_lock`` free."""
        from cw.cli import _spawn_close_impl

        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            status=SessionStatus.COMPLETED,
            surface_ref="deadbeef",
        )
        daemon = LockProbeDaemon()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        assert daemon.probes == [("free", {sess.id: SessionStatus.COMPLETED})]
        assert daemon.stop_calls == ["deadbeef"]

    def test_cancel_and_stamp_land_before_stop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Order is cancel -> stamp -> release -> stop (mirrors ``signal_stop``)."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = _SpawnStopProbeDaemon()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        assert daemon.queue_statuses == [{"GEN-42": QueueItemStatus.CANCELLED}]
        assert daemon.probes == [("free", {sess.id: SessionStatus.COMPLETED})]

    def test_stop_failure_after_stamp_leaves_session_completed_and_retry_succeeds(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A raising stop no longer strands an un-stamped session: the state is
        already COMPLETED / CANCELLED, and a repeat close retries only the stop
        (#2480's already-COMPLETED branch)."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = _SpawnStopProbeDaemon(raise_first_stop=True)

        with pytest.raises(OSError, match="daemon socket gone"):
            _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        stamped = load_state().find_by_name_or_id(sess.id)
        assert stamped is not None
        assert stamped.status == SessionStatus.COMPLETED
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        assert daemon.stop_calls == ["abc12345", "abc12345"]

    # -- #2517: surface_already_stopped suppresses the post-lock stop ------

    def test_surface_already_stopped_flips_live_session_without_stop(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The caller already stopped (or proved absent) the worker, so the
        cancel and stamp land as usual and no second stop is made -- and no
        daemon client is resolved for one."""
        from cw.cli import _spawn_close_impl

        def _no_client() -> NativeDaemonClient:
            pytest.fail("surface_already_stopped must not resolve a daemon client")

        monkeypatch.setattr("cw.cli.spawn.get_native_daemon_client", _no_client)
        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = LockProbeDaemon()

        _spawn_close_impl(
            session_id=sess.id, native_daemon=daemon, surface_already_stopped=True
        )
        _spawn_close_impl(session_id=sess.id, surface_already_stopped=True)

        assert daemon.probes == []
        assert daemon.stop_calls == []
        closed = load_state().find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED

    def test_surface_already_stopped_on_completed_session_makes_no_stop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2480's already-COMPLETED branch also skips its stop."""
        from cw.cli import _spawn_close_impl

        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            status=SessionStatus.COMPLETED,
            surface_ref="deadbeef",
        )
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(
            session_id=sess.id, native_daemon=daemon, surface_already_stopped=True
        )

        assert daemon.stop_calls == []

    # -- #2458: a staged emit_cli result is routed, not thrown away ---------

    _CLOSE_TICKET = "GEN-1234"

    def _seed_close_row(self, session_id: str, stage: Stage) -> None:
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus

        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id=self._CLOSE_TICKET,
                        client="test-client",
                        status=QueueItemStatus.RUNNING,
                        session_id=session_id,
                        stage=stage,
                        attempts=1,
                    )
                ]
            )
        )

    @staticmethod
    def _write_staged_client() -> None:
        from cw.config import clients_file

        path = clients_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "clients:\n"
            "  test-client:\n"
            "    workspace_path: /tmp/ws-close-2458\n"
            "    default_branch: main\n"
            "    pipeline:\n"
            "      stages: [plan, impl, review, finalize]\n"
        )

    @staticmethod
    def _reload_close_row() -> TicketTask:
        from cw.dev_queue import load_dev_queue

        return next(
            t
            for t in load_dev_queue().tasks
            if t.ticket_id == TestSpawnClose._CLOSE_TICKET
        )

    def test_spawn_close_routes_staged_emit_cli_result_instead_of_cancelling(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A worker that already emitted ``shipped`` has its row COMPLETED on
        close -- not CANCELLED, which would force a manual
        ``requeue --from-cancelled`` over already-validated work."""
        from cw.cli import _spawn_close_impl
        from cw.models import LastResultSource, QueueItemStatus
        from tests.test_result import _valid_payload

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        stored.last_result = _valid_payload()
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        self._write_staged_client()
        self._seed_close_row(sess.id, Stage.FINALIZE)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        row = self._reload_close_row()
        assert row.status == QueueItemStatus.COMPLETED
        closed = load_state().find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.last_result == _valid_payload()
        assert closed.last_result_source == LastResultSource.EMIT_CLI

    def test_spawn_close_advances_staged_non_terminal_stage_complete(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A staged non-terminal ``stage_complete`` advances the row to its
        next stage on close -- the close path routes a genuine mid-pipeline
        advance, not only a terminal ``shipped``, and never cancels it."""
        from cw.cli import _spawn_close_impl
        from cw.models import LastResultSource, QueueItemStatus
        from tests._reconcile_helpers import _stage_complete_payload

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        stored.last_result = _stage_complete_payload()
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        self._write_staged_client()
        self._seed_close_row(sess.id, Stage.IMPL)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        row = self._reload_close_row()
        assert row.stage == Stage.REVIEW
        assert row.status == QueueItemStatus.PENDING
        closed = load_state().find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER

    def test_spawn_close_falls_back_to_cancel_when_nothing_staged(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Regression: no staged result -> the #317 cancel runs as before."""
        from cw.cli import _spawn_close_impl
        from cw.models import QueueItemStatus

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        self._seed_close_row(sess.id, Stage.FINALIZE)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        row = self._reload_close_row()
        assert row.status == QueueItemStatus.CANCELLED
        assert row.session_id is None

    def test_spawn_close_cancels_when_staged_result_is_refused(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A staged result the routing authority refuses (a #1031 stage
        mismatch) leaves the row RUNNING, so the #317 cancel still runs."""
        from cw.cli import _spawn_close_impl
        from cw.models import LastResultSource, QueueItemStatus
        from tests._reconcile_helpers import _stage_complete_payload

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        # stage2_impl reported against a row already at FINALIZE.
        stored.last_result = _stage_complete_payload()
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        self._write_staged_client()
        self._seed_close_row(sess.id, Stage.FINALIZE)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        assert self._reload_close_row().status == QueueItemStatus.CANCELLED

    def test_spawn_close_cancels_when_emit_cli_result_is_not_terminal(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """An emit_cli-sourced ``last_result`` with no ``status`` (a merged
        park marker) is not a staged result -> cancel."""
        from cw.cli import _spawn_close_impl
        from cw.models import LastResultSource, QueueItemStatus

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        stored.last_result = {"paused_status": "silently_idle"}
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        self._seed_close_row(sess.id, Stage.FINALIZE)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        assert self._reload_close_row().status == QueueItemStatus.CANCELLED

    def test_spawn_close_with_staged_result_but_no_running_row_still_closes(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No RUNNING row owns the session -> nothing to route; the (no-op)
        cancel runs and the session closes as before."""
        from cw.cli import _spawn_close_impl
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, LastResultSource
        from tests.test_result import _valid_payload

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        stored.last_result = _valid_payload()
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        closed = load_state().find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED

    def test_spawn_close_cancels_when_staged_result_is_unreconstructable(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A malformed staged dict carries nothing to route -> cancel."""
        from cw.cli import _spawn_close_impl
        from cw.models import LastResultSource, QueueItemStatus

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        state = load_state()
        stored = state.find_by_name_or_id(sess.id)
        assert stored is not None
        stored.last_result = {"status": "shipped"}
        stored.last_result_source = LastResultSource.EMIT_CLI
        save_state(state)
        self._seed_close_row(sess.id, Stage.FINALIZE)

        _spawn_close_impl(session_id=sess.id, native_daemon=FakeNativeDaemonClient())

        assert self._reload_close_row().status == QueueItemStatus.CANCELLED


# ---------------------------------------------------------------------------
# TestCloseRoutedResultSessionsForTicket (#2517)
# ---------------------------------------------------------------------------

_ORPHAN_TICKET = "2517"
_ORPHAN_CLIENT = "client-a"
_ORPHAN_NAME = f"{_ORPHAN_CLIENT}/auto-dev/{_ORPHAN_TICKET}"


def _seed_orphans(
    tmp_path: Path,
    daemon: FakeNativeDaemonClient,
    *sids: str,
    live: bool = True,
    extra: tuple[Session, ...] = (),
) -> list[Session]:
    """Persist marker sessions for the #2517 ticket (plus *extra*); return them.

    Hermetic (N5): each worktree lives under *tmp_path*, and a live session's
    surface is the short id ``seed_live_worker(worktree)`` registered.
    """
    sessions: list[Session] = []
    for sid in sids or ("orphan-1",):
        worktree = tmp_path / f"wt-{sid}"
        surface = daemon.seed_live_worker(worktree) if live else f"gone-{sid}"
        sess = _mk_routed_session(sid, worktree, surface_ref=surface)
        sess.name = _ORPHAN_NAME
        sessions.append(sess)
    save_state(CwState(sessions=[*sessions, *extra]))
    return sessions


def _bystander(tmp_path: Path, *, surface_ref: str = "bystander") -> Session:
    """A live DAEMON session for another ticket, not a #2517 candidate."""
    return _make_daemon_session(
        id="bystander",
        name=f"{_ORPHAN_CLIENT}/auto-dev/9999",
        client=_ORPHAN_CLIENT,
        worktree_path=tmp_path / "wt-bystander",
        surface_ref=surface_ref,
    )


def _close_orphans(
    daemon: NativeDaemonClient | None,
    *,
    after_transition: bool,
    precheck: Callable[[frozenset[str]], None] | None = None,
) -> list[str]:
    from cw.cli.spawn import close_routed_result_sessions_for_ticket

    return close_routed_result_sessions_for_ticket(
        _ORPHAN_TICKET,
        _ORPHAN_CLIENT,
        command="approve" if after_transition else "requeue",
        config=OrchestratorConfig(),
        after_transition=after_transition,
        native_daemon=daemon,
        precheck=precheck,
    )


def _status_of(session_id: str) -> SessionStatus:
    sess = next(s for s in load_state().sessions if s.id == session_id)
    return sess.status


def _orphan_records(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(f"{event}:")]


def _flat(text: str) -> str:
    return " ".join(text.split())


_AFTER_TRANSITION = pytest.mark.parametrize("after_transition", [False, True])


class TestCloseRoutedResultSessionsForTicket:
    """approve/requeue close an orphaned routed-result session (#2517)."""

    @pytest.fixture(autouse=True)
    def _instant_roster_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cw.cli.spawn._ROUTED_STOP_CONFIRM_TIMEOUT_SECS", 0.0)
        monkeypatch.setattr("cw.cli.spawn._ROUTED_STOP_CONFIRM_INTERVAL_SECS", 0.0)

    # -- the close itself -------------------------------------------------

    @_AFTER_TRANSITION
    def test_closes_matching_orphan_once_outside_the_lock(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
        after_transition: bool,
    ) -> None:
        daemon = LockProbeDaemon()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            closed = _close_orphans(daemon, after_transition=after_transition)

        assert closed == [orphan.id]
        assert daemon.stop_calls == [orphan.surface_ref]
        assert daemon.probes == [("free", {orphan.id: SessionStatus.ACTIVE})]
        stored = next(s for s in load_state().sessions if s.id == orphan.id)
        assert stored.status == SessionStatus.COMPLETED
        assert stored.completed_reason == CompletionReason.USER
        (record,) = _orphan_records(caplog, "routed_orphan_closed_on_resolve")
        assert record.levelno == logging.WARNING
        command = "approve" if after_transition else "requeue"
        assert record.getMessage() == (
            "routed_orphan_closed_on_resolve: ticket_id=2517 client=client-a"
            f" session_id={orphan.id} command={command}"
        )
        out = capsys.readouterr().out
        assert (
            f"Closed orphaned routed-result session {orphan.id} for 2517 (client-a)."
            in out
        )

    def test_leaves_unrelated_sessions_and_other_tickets_alone(
        self, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        other_ticket = _mk_routed_session(
            "other-ticket",
            tmp_path / "wt-other-ticket",
            surface_ref=daemon.seed_live_worker(tmp_path / "wt-other-ticket"),
        )
        other_client = _mk_routed_session(
            "other-client",
            tmp_path / "wt-other-client",
            surface_ref=daemon.seed_live_worker(tmp_path / "wt-other-client"),
        )
        other_client.name = f"client-b/auto-dev/{_ORPHAN_TICKET}"
        other_client.client = "client-b"
        (orphan,) = _seed_orphans(tmp_path, daemon, extra=(other_ticket, other_client))

        assert _close_orphans(daemon, after_transition=True) == [orphan.id]

        assert daemon.stop_calls == [orphan.surface_ref]
        assert _status_of("other-ticket") == SessionStatus.ACTIVE
        assert _status_of("other-client") == SessionStatus.ACTIVE

    def test_no_candidates_touches_no_daemon_roster_or_queue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> NativeDaemonClient:
            pytest.fail("no candidate: nothing may be resolved or read")

        class _NoRosterDaemon(FakeNativeDaemonClient):
            def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
                pytest.fail("no candidate: the roster must not be read")

        monkeypatch.setattr("cw.cli.spawn.get_native_daemon_client", _fail)
        monkeypatch.setattr("cw.cli.spawn.load_dev_queue", _fail)
        save_state(CwState(sessions=[_bystander(tmp_path)]))
        precheck_calls: list[frozenset[str]] = []

        assert _close_orphans(None, after_transition=True) == []
        assert (
            _close_orphans(
                _NoRosterDaemon(),
                after_transition=False,
                precheck=precheck_calls.append,
            )
            == []
        )

        assert precheck_calls == []
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""

    def test_lazy_client_resolution_with_a_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        monkeypatch.setattr("cw.cli.spawn.get_native_daemon_client", lambda: daemon)

        assert _close_orphans(None, after_transition=True) == [orphan.id]
        assert daemon.stop_calls == [orphan.surface_ref]

    def test_queue_rows_are_never_written(self, tmp_path: Path) -> None:
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore

        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id=_ORPHAN_TICKET,
                        client=_ORPHAN_CLIENT,
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id=orphan.id,
                    )
                ]
            )
        )
        before = load_dev_queue().model_dump()

        assert _close_orphans(daemon, after_transition=False) == [orphan.id]

        assert load_dev_queue().model_dump() == before

    # -- unreadable / untrusted roster before any stop ---------------------

    def test_roster_unreadable_before_stop_closes_nothing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        daemon.roster_unreadable = True

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=False) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        err = capsys.readouterr().err
        assert orphan.id in err
        assert "the daemon roster is unreadable" in err
        assert close_command(orphan.id) in err
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert record.levelno == logging.WARNING
        assert f"session_ids={orphan.id}" in record.getMessage()
        assert "reason=roster_unreadable" in record.getMessage()

    def test_roster_read_error_is_treated_as_unreadable(self, tmp_path: Path) -> None:
        class _RaisingRosterDaemon(FakeNativeDaemonClient):
            def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
                msg = "malformed roster"
                raise ValueError(msg)

        daemon = _RaisingRosterDaemon()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        assert _close_orphans(daemon, after_transition=True) == []
        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE

    @_AFTER_TRANSITION
    def test_roster_absent_orphan_is_flipped_without_a_stop(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        after_transition: bool,
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon, live=False)
        assert orphan.worktree_path is not None
        assert live_home_reason(orphan.worktree_path, daemon=daemon) is not None

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            closed = _close_orphans(daemon, after_transition=after_transition)

        assert closed == [orphan.id]
        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.COMPLETED
        (record,) = _orphan_records(caplog, "routed_orphan_closed_on_resolve")
        assert record.levelno == logging.WARNING
        assert live_home_reason(orphan.worktree_path, daemon=daemon) is None

    def test_flip_only_skipped_when_two_roster_reads_disagree(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A roster absence must be corroborated by a second read (soundness
        RISK: a partial roster mid-rewrite must not free the worktree)."""

        class _PartialRosterDaemon(FakeNativeDaemonClient):
            def __init__(self, reads: list[set[str]]) -> None:
                super().__init__()
                self.reads = reads

            def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
                return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

        (orphan,) = _seed_orphans(tmp_path, FakeNativeDaemonClient(), live=False)
        assert orphan.surface_ref is not None
        daemon = _PartialRosterDaemon(
            [{"unrelated"}, {"unrelated"}, {"unrelated", orphan.surface_ref}]
        )

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=False) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        err = capsys.readouterr().err
        assert "changed between two reads" in err
        assert close_command(orphan.id) in err
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=roster_unreadable" in record.getMessage()

    def test_empty_roster_with_other_live_session_is_not_flipped(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """N1 (a): an empty roster while another session is recorded live is
        the daemon-restart signature, never proof the worker is gone."""
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(
            tmp_path, daemon, live=False, extra=(_bystander(tmp_path),)
        )

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=False) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        err = capsys.readouterr().err
        assert orphan.id in err
        assert "looks like a daemon restart" in err
        assert close_command(orphan.id) in err
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=roster_unreadable" in record.getMessage()

    def test_absent_roster_file_with_other_live_session_is_not_flipped(
        self, tmp_path: Path
    ) -> None:
        """N1 (b): an absent roster file reads as an empty set."""
        from cw.native_daemon import RealNativeDaemonClient

        (orphan,) = _seed_orphans(
            tmp_path,
            FakeNativeDaemonClient(),
            live=False,
            extra=(_bystander(tmp_path),),
        )
        daemon = RealNativeDaemonClient(roster_path=tmp_path / "missing.json")

        assert _close_orphans(daemon, after_transition=True) == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE

    # -- the post-stop roster check (N1 + the pre-stop snapshot) -----------

    def test_stop_that_empties_whole_roster_with_other_live_worker_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N1 (d): a roster that held another live worker before the stop and
        reads empty after it is the restart signature, not a confirmation."""
        daemon = FakeNativeDaemonClient()
        bystander = _bystander(
            tmp_path, surface_ref=daemon.seed_live_worker(tmp_path / "wt-bystander")
        )
        (orphan,) = _seed_orphans(tmp_path, daemon, extra=(bystander,))

        def _wipe(short_id: str) -> None:
            daemon.stop_calls.append(short_id)
            daemon._live.clear()

        monkeypatch.setattr(daemon, "stop", _wipe)

        with pytest.raises(CwError, match="unreadable after the stop"):
            _close_orphans(daemon, after_transition=False)

        assert daemon.stop_calls == [orphan.surface_ref]
        assert _status_of(orphan.id) == SessionStatus.ACTIVE

    def test_stale_unrelated_session_and_lone_orphan_worker_closes_cleanly(
        self, tmp_path: Path
    ) -> None:
        """Soundness note: a stale ACTIVE session whose worker is long gone
        must not turn the orphan's own stop -- which empties a roster that only
        ever held the orphan -- into a refusal that can never recover."""
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon, extra=(_bystander(tmp_path),))

        assert _close_orphans(daemon, after_transition=False) == [orphan.id]

        assert daemon.stop_calls == [orphan.surface_ref]
        assert _status_of(orphan.id) == SessionStatus.COMPLETED
        assert _status_of("bystander") == SessionStatus.ACTIVE

    def test_mixed_stop_and_flip_only_candidates_both_close(
        self, tmp_path: Path
    ) -> None:
        """N1 (e): the outage exclusion is the whole candidate set, so the
        still-ACTIVE flip-only candidate never makes the stop look like an
        outage."""
        daemon = FakeNativeDaemonClient()
        live, absent = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
        assert absent.surface_ref is not None
        daemon._live.discard(absent.surface_ref)

        closed = _close_orphans(daemon, after_transition=False)

        assert sorted(closed) == ["orphan-a", "orphan-b"]
        assert daemon.stop_calls == [live.surface_ref]
        assert _status_of("orphan-a") == SessionStatus.COMPLETED
        assert _status_of("orphan-b") == SessionStatus.COMPLETED

    def test_mixed_candidates_with_third_live_worker_wiped_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control for (e): a non-candidate live worker that vanishes with the
        stop makes the empty roster outage-shaped again."""
        daemon = FakeNativeDaemonClient()
        bystander = _bystander(
            tmp_path, surface_ref=daemon.seed_live_worker(tmp_path / "wt-bystander")
        )
        live, absent = _seed_orphans(
            tmp_path, daemon, "orphan-a", "orphan-b", extra=(bystander,)
        )
        assert absent.surface_ref is not None
        daemon._live.discard(absent.surface_ref)

        def _wipe(short_id: str) -> None:
            daemon.stop_calls.append(short_id)
            daemon._live.clear()

        monkeypatch.setattr(daemon, "stop", _wipe)

        with pytest.raises(CwError, match="unreadable after the stop"):
            _close_orphans(daemon, after_transition=False)

        assert daemon.stop_calls == [live.surface_ref]
        assert _status_of("orphan-a") == SessionStatus.ACTIVE

    def test_roster_unreadable_right_after_confirming_wait_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """N1 (f): the extra post-wait read returning None is unconfirmed."""
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _confirm_then_break(*_args: object, **_kwargs: object) -> bool:
            daemon.roster_unreadable = True
            return True

        monkeypatch.setattr(
            "cw.cli.spawn.wait_for_roster_presence", _confirm_then_break
        )

        with (
            caplog.at_level(logging.WARNING, logger="cw.cli.spawn"),
            pytest.raises(CwError, match="unreadable after the stop"),
        ):
            _close_orphans(daemon, after_transition=False)

        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_stop_unconfirmed")
        assert "reason=roster_unreadable_after_stop" in record.getMessage()

    def test_wait_that_raises_is_unconfirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _raise(*_args: object, **_kwargs: object) -> bool:
            msg = "roster vanished"
            raise OSError(msg)

        monkeypatch.setattr("cw.cli.spawn.wait_for_roster_presence", _raise)

        with pytest.raises(CwError, match="still listed in the roster"):
            _close_orphans(daemon, after_transition=False)

        assert _status_of(orphan.id) == SessionStatus.ACTIVE

    # -- pins -------------------------------------------------------------

    @_AFTER_TRANSITION
    def test_running_row_pins_in_both_modes(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
        after_transition: bool,
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        _seed_running_task(_ORPHAN_TICKET, _ORPHAN_CLIENT, orphan.id)

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=after_transition) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        err = capsys.readouterr().err
        assert err.count("pinned by") == 1
        assert "pinned by 2517 (client-a, RUNNING)" in err
        assert close_command(orphan.id) in err
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=pinned" in record.getMessage()

    @pytest.mark.parametrize(
        ("after_transition", "closes"), [(False, True), (True, False)]
    )
    def test_own_parked_row_pins_only_after_the_transition(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        after_transition: bool,
        closes: bool,
    ) -> None:
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore

        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id=_ORPHAN_TICKET,
                        client=_ORPHAN_CLIENT,
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id=orphan.id,
                    )
                ]
            )
        )

        closed = _close_orphans(daemon, after_transition=after_transition)

        assert closed == ([orphan.id] if closes else [])
        if not closes:
            err = capsys.readouterr().err
            assert "pinned by 2517 (client-a, BLOCKED_ON_USER)" in err

    @_AFTER_TRANSITION
    def test_other_tickets_parked_row_pins(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        after_transition: bool,
    ) -> None:
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore

        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="9999",
                        client=_ORPHAN_CLIENT,
                        status=QueueItemStatus.BLOCKED_ON_USER,
                        session_id=orphan.id,
                    )
                ]
            )
        )

        assert _close_orphans(daemon, after_transition=after_transition) == []

        assert daemon.stop_calls == []
        assert "pinned by 9999 (client-a, BLOCKED_ON_USER)" in capsys.readouterr().err

    # -- draining ---------------------------------------------------------

    def _stamp(self, sess: Session, *, minutes_ago: float) -> None:
        assert sess.worktree_path is not None
        _write_agent_spawn_stamp(
            sess.worktree_path,
            unresolved_count=1,
            stamped_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        )

    def test_draining_refuses_requeue_and_stops_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon = FakeNativeDaemonClient()
        draining, idle = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
        self._stamp(draining, minutes_ago=5)

        with (
            caplog.at_level(logging.WARNING, logger="cw.cli.spawn"),
            pytest.raises(CwError) as excinfo,
        ):
            _close_orphans(daemon, after_transition=False)

        message = _flat(str(excinfo.value))
        assert "background work is still draining" in message
        assert "The row was not requeued." in message
        assert close_command(draining.id) in message
        assert daemon.stop_calls == []
        assert _status_of(draining.id) == SessionStatus.ACTIVE
        assert _status_of(idle.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_close_refused")
        assert "reason=background_work_draining" in record.getMessage()

    def test_stale_spawn_stamp_is_closed_normally(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        self._stamp(orphan, minutes_ago=90)

        assert _close_orphans(daemon, after_transition=False) == [orphan.id]

    def test_draining_is_left_running_after_approve(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        daemon = FakeNativeDaemonClient()
        draining, idle = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
        self._stamp(draining, minutes_ago=5)

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=True) == [idle.id]

        assert daemon.stop_calls == [idle.surface_ref]
        assert _status_of(draining.id) == SessionStatus.ACTIVE
        err = _flat(capsys.readouterr().err)
        assert f"Left running: routed-result session {draining.id}" in err
        assert "background work is still draining" in err
        assert close_command(draining.id) in err
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=background_work_draining" in record.getMessage()

    # -- a stop that does not take (R4) -----------------------------------

    @_AFTER_TRANSITION
    def test_stop_that_leaves_worker_listed_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        after_transition: bool,
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        _stop_leaves_worker_listed(monkeypatch, daemon)
        before = load_dev_queue().model_dump()

        with (
            caplog.at_level(logging.WARNING, logger="cw.cli.spawn"),
            pytest.raises(CwError) as excinfo,
        ):
            _close_orphans(daemon, after_transition=after_transition)

        message = _flat(str(excinfo.value))
        assert orphan.id in message
        assert f"claude stop {orphan.surface_ref}" in message
        assert str(daemon.roster_path) in message
        assert "still listed in the roster" in message
        if after_transition:
            assert message.startswith("Approved 2517 (client-a)")
            assert "row is released" in message
            assert "Do NOT re-run approve" in message
            assert close_command(orphan.id) in message
        else:
            assert "The row was not requeued." in message
            assert "status flip alone" in message
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_stop_unconfirmed")
        assert "reason=worker_still_listed" in record.getMessage()
        assert _orphan_records(caplog, "routed_orphan_closed_on_resolve") == []
        assert load_dev_queue().model_dump() == before

    def test_first_closed_stays_closed_when_second_stop_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        daemon = FakeNativeDaemonClient()
        first, second = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")

        def _stop_first_only(short_id: str) -> None:
            daemon.stop_calls.append(short_id)
            if short_id == first.surface_ref:
                daemon._live.discard(short_id)

        monkeypatch.setattr(daemon, "stop", _stop_first_only)

        with pytest.raises(CwError, match=second.id):
            _close_orphans(daemon, after_transition=False)

        assert _status_of(first.id) == SessionStatus.COMPLETED
        assert _status_of(second.id) == SessionStatus.ACTIVE
        assert f"Closed orphaned routed-result session {first.id}" in (
            capsys.readouterr().out
        )

    def test_retry_after_late_roster_removal_closes_by_flip_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        assert orphan.surface_ref is not None
        _stop_leaves_worker_listed(monkeypatch, daemon)
        with pytest.raises(CwError):
            _close_orphans(daemon, after_transition=False)

        daemon._live.discard(orphan.surface_ref)

        assert _close_orphans(daemon, after_transition=False) == [orphan.id]
        assert daemon.stop_calls == [orphan.surface_ref]
        assert _status_of(orphan.id) == SessionStatus.COMPLETED

    @_AFTER_TRANSITION
    def test_flip_error_after_confirmed_stop_refuses_then_retry_flips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        after_transition: bool,
    ) -> None:
        from cw.cli import spawn as spawn_cli

        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        real_close = spawn_cli._spawn_close_impl
        calls: list[str] = []

        def _fail_once(*, session_id: str, **kwargs: object) -> None:
            calls.append(session_id)
            if len(calls) == 1:
                msg = "sessions lock timed out"
                raise CwError(msg)
            real_close(session_id=session_id, **kwargs)

        monkeypatch.setattr("cw.cli.spawn._spawn_close_impl", _fail_once)

        with (
            caplog.at_level(logging.WARNING, logger="cw.cli.spawn"),
            pytest.raises(CwError) as excinfo,
        ):
            _close_orphans(daemon, after_transition=after_transition)

        message = _flat(str(excinfo.value))
        assert close_command(orphan.id) in message
        assert "sessions lock timed out" in message
        (record,) = _orphan_records(caplog, "routed_orphan_close_failed")
        assert "error=sessions lock timed out" in record.getMessage()
        assert _status_of(orphan.id) == SessionStatus.ACTIVE

        assert _close_orphans(daemon, after_transition=after_transition) == [orphan.id]
        assert daemon.stop_calls == [orphan.surface_ref]
        assert _status_of(orphan.id) == SessionStatus.COMPLETED

    def test_stop_that_makes_roster_unreadable_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        _stop_makes_roster_unreadable(monkeypatch, daemon)

        with (
            caplog.at_level(logging.WARNING, logger="cw.cli.spawn"),
            pytest.raises(CwError, match="unreadable after the stop"),
        ):
            _close_orphans(daemon, after_transition=False)

        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_stop_unconfirmed")
        assert "reason=roster_unreadable_after_stop" in record.getMessage()

    # -- pre-stop re-validation (R2): precheck is the injection point -------

    def test_revalidation_sees_a_new_running_pin(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _bind(_ids: frozenset[str]) -> None:
            _seed_running_task(_ORPHAN_TICKET, _ORPHAN_CLIENT, orphan.id)

        assert _close_orphans(daemon, after_transition=False, precheck=_bind) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        assert "pinned by 2517" in capsys.readouterr().err

    def test_revalidation_sees_fresh_draining_stamp(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _stamp(_ids: frozenset[str]) -> None:
            self._stamp(orphan, minutes_ago=1)

        with pytest.raises(CwError, match="background work is still draining"):
            _close_orphans(daemon, after_transition=False, precheck=_stamp)

        assert daemon.stop_calls == []

    def test_revalidation_skips_a_session_no_longer_a_candidate(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _complete(_ids: frozenset[str]) -> None:
            state = load_state()
            state.sessions[0].status = SessionStatus.COMPLETED
            save_state(state)

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert (
                _close_orphans(daemon, after_transition=False, precheck=_complete) == []
            )

        assert daemon.stop_calls == []
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=no_longer_candidate" in record.getMessage()
        assert orphan.id in record.getMessage()

    def test_revalidation_skips_when_roster_turns_unreadable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _break(_ids: frozenset[str]) -> None:
            daemon.roster_unreadable = True

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=False, precheck=_break) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=roster_unreadable_before_stop" in record.getMessage()

    def test_revalidation_reclassifies_an_emptied_roster_as_untrusted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon, extra=(_bystander(tmp_path),))

        def _empty(_ids: frozenset[str]) -> None:
            daemon._live.clear()

        with caplog.at_level(logging.WARNING, logger="cw.cli.spawn"):
            assert _close_orphans(daemon, after_transition=False, precheck=_empty) == []

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE
        (record,) = _orphan_records(caplog, "routed_orphan_close_skipped")
        assert "reason=roster_unreadable" in record.getMessage()

    def test_precheck_refusal_propagates_before_any_stop(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)

        def _refuse(_ids: frozenset[str]) -> None:
            msg = "requeue would refuse"
            raise CwError(msg)

        with pytest.raises(CwError, match="requeue would refuse"):
            _close_orphans(daemon, after_transition=False, precheck=_refuse)

        assert daemon.stop_calls == []
        assert _status_of(orphan.id) == SessionStatus.ACTIVE

    def test_precheck_gets_exactly_the_stop_and_flip_only_ids(
        self, tmp_path: Path
    ) -> None:
        daemon = FakeNativeDaemonClient()
        live, absent, pinned = _seed_orphans(
            tmp_path, daemon, "orphan-a", "orphan-b", "orphan-c"
        )
        assert absent.surface_ref is not None
        daemon._live.discard(absent.surface_ref)
        _seed_running_task("9999", _ORPHAN_CLIENT, pinned.id)
        calls: list[frozenset[str]] = []

        _close_orphans(daemon, after_transition=False, precheck=calls.append)

        assert calls == [frozenset({live.id, absent.id})]

    def test_precheck_not_called_without_targets(self, tmp_path: Path) -> None:
        daemon = FakeNativeDaemonClient()
        (orphan,) = _seed_orphans(tmp_path, daemon)
        _seed_running_task(_ORPHAN_TICKET, _ORPHAN_CLIENT, orphan.id)
        calls: list[frozenset[str]] = []

        assert (
            _close_orphans(daemon, after_transition=False, precheck=calls.append) == []
        )
        assert calls == []


# ---------------------------------------------------------------------------
# TestSpawnComplete
# ---------------------------------------------------------------------------


class TestSpawnComplete:
    """Tests for _spawn_complete_impl and cw spawn complete command."""

    def test_held_dev_queue_lock_times_out_before_any_side_effect(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nested dev-queue lock is bounded too (#2501): a clean retry."""
        from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
        from cw.cli import _spawn_complete_impl
        from cw.config import dev_queue_file, dev_queue_lock
        from cw.events import read_events
        from cw.exceptions import LockTimeoutError
        from cw.models import OrchestratorEventType
        from tests.conftest import _hold_flock

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        queue_before = dev_queue_file().read_bytes()
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.05")
        daemon = FakeNativeDaemonClient()

        with (
            _hold_flock(dev_queue_lock()),
            pytest.raises(LockTimeoutError) as exc_info,
        ):
            _spawn_complete_impl(
                session_id=sess.id,
                status="shipped",
                ticket_id=None,
                force=False,
                native_daemon=daemon,
            )

        assert exc_info.value.lock_name == "dev_queue"
        events = read_events(
            consumer="_test_held_dev_queue",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert events == []
        updated = load_state().find_by_name_or_id(sess.id)
        assert updated is not None
        assert updated.status == SessionStatus.ACTIVE
        assert dev_queue_file().read_bytes() == queue_before
        assert daemon.stop_calls == []
        assert held_locks() == ()

    def test_nested_timeout_reports_only_its_own_wait(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each bounded lock waits up to the timeout on its own (#2501)."""
        from cw import _flock
        from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
        from cw.cli import _spawn_complete_impl
        from cw.config import dev_queue_lock, sessions_lock_file
        from cw.exceptions import LockTimeoutError
        from tests.conftest import _FakeClock, _hold_flock

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.5")

        with (
            _hold_flock(dev_queue_lock()),
            _hold_flock(sessions_lock_file()) as release_sessions,
        ):
            # The sessions holder lets go at the first poll sleep, so the
            # outer acquisition succeeds after one sleep; dev_queue stays held.
            clock = _FakeClock(on_sleep=lambda _n: release_sessions())
            monkeypatch.setattr(_flock, "time", clock)
            with pytest.raises(LockTimeoutError) as exc_info:
                _spawn_complete_impl(
                    session_id=sess.id,
                    status="shipped",
                    ticket_id=None,
                    force=False,
                    native_daemon=FakeNativeDaemonClient(),
                )

        err = exc_info.value
        assert err.lock_name == "dev_queue"
        assert sum(clock.sleeps) > err.waited_s
        assert err.waited_s == pytest.approx(0.5)
        assert str(err).startswith("Timed out after 0.5s")

    def test_spawn_close_still_waits_out_a_held_dev_queue_lock(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cancel_task_for_session`` runs before the post-lock stop: unbounded."""
        import threading

        from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
        from cw.cli import _spawn_close_impl
        from cw.config import dev_queue_lock
        from cw.dev_queue import load_dev_queue
        from cw.models import QueueItemStatus
        from tests.conftest import _hold_flock

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.01")

        with _hold_flock(dev_queue_lock()) as release:
            timer = threading.Timer(0.2, release)
            timer.start()
            try:
                _spawn_close_impl(
                    session_id=sess.id, native_daemon=FakeNativeDaemonClient()
                )
            finally:
                timer.cancel()

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED

    def test_spawn_close_requeue_keeps_the_unbounded_default(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--requeue`` runs after the close landed: no ``bounded`` passed."""
        from cw.cli.spawn import _spawn_close_requeue_impl

        seen: list[dict[str, object]] = []

        def _record(*_args: object, **kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {"from_stage": "plan", "to_stage": "plan", "regressed": False}

        monkeypatch.setattr("cw.cli.spawn.requeue_ticket", _record)

        _spawn_close_requeue_impl(
            session_id="dead1234", ticket_id="GEN-42", client="test-client"
        )

        assert len(seen) == 1
        assert "bounded" not in seen[0]

    def test_happy_path_session_completed_with_reason_user(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Happy path: session COMPLETED with reason=USER, queue task COMPLETED."""
        from cw.cli import _spawn_complete_impl

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,
            force=False,
            native_daemon=daemon,
        )

        state = load_state()
        updated = state.find_by_name_or_id(sess.id)
        assert updated is not None
        assert updated.status == SessionStatus.COMPLETED
        assert updated.completed_reason == CompletionReason.USER
        assert updated.completed_at is not None

        from cw.dev_queue import load_dev_queue
        from cw.models import QueueItemStatus

        store = load_dev_queue()
        task = next((t for t in store.tasks if t.ticket_id == "GEN-42"), None)
        assert task is not None
        # B2: no last_result on session -> Rule 6 -> BLOCKED_ON_USER
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_happy_path_event_recorded_with_correct_payload(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Happy path: SESSION_COMPLETED event recorded with correct payload."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,
            force=False,
            native_daemon=daemon,
        )

        events = read_events(
            consumer="_test_consumer",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] == sess.id
        assert payload["client"] == "test-client"
        assert payload["crashed"] is False
        assert payload["status"] == "shipped"
        assert payload["ticket_id"] == "GEN-42"

    def test_ticket_id_inferred_from_session_name_when_omitted(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """ticket_id inferred from session name when not provided."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        # Name encodes ticket_id via AUTO_DEV_LABEL_PREFIX pattern
        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, name="test-client/auto-dev/GEN-99"
        )
        _seed_running_task(ticket_id="GEN-99", client="test-client", session_id=sess.id)
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,  # omitted — must be inferred
            force=False,
            native_daemon=daemon,
        )

        events = read_events(
            consumer="_test_consumer2",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(e.payload.get("ticket_id") == "GEN-99" for e in events)

    def test_explicit_ticket_id_overrides_inferred(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Explicit --ticket-id overrides whatever the session name encodes."""
        from cw.cli import _spawn_complete_impl
        from cw.dev_queue import load_dev_queue
        from cw.models import QueueItemStatus

        # Session name would infer GEN-42
        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, name="test-client/auto-dev/GEN-42"
        )
        # But we seed the queue with a different ticket_id
        _seed_running_task(
            ticket_id="OVERRIDE-1", client="test-client", session_id=sess.id
        )
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id="OVERRIDE-1",  # explicit override
            force=False,
            native_daemon=daemon,
        )

        store = load_dev_queue()
        task = next((t for t in store.tasks if t.ticket_id == "OVERRIDE-1"), None)
        assert task is not None
        # B2: no last_result on session -> Rule 6 -> BLOCKED_ON_USER
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_already_completed_session_raises_without_force(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Session already COMPLETED → CwError without --force."""
        from cw.cli import _spawn_complete_impl

        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, status=SessionStatus.COMPLETED
        )
        daemon = FakeNativeDaemonClient()

        with pytest.raises(CwError, match="already completed"):
            _spawn_complete_impl(
                session_id=sess.id,
                status="shipped",
                ticket_id=None,
                force=False,
                native_daemon=daemon,
            )

    def test_already_completed_session_force_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Session already COMPLETED + --force -> no new SESSION_COMPLETED
        event, but #2480: the lingering daemon surface IS still stopped."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            status=SessionStatus.COMPLETED,
            surface_ref="deadbeef",
        )
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,
            force=True,  # --force
            native_daemon=daemon,
        )

        events = read_events(
            consumer="_test_consumer3",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 0
        assert daemon.stop_calls == ["deadbeef"]

    def test_force_noop_stop_runs_after_sessions_lock_released(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """#2547: the ``--force`` no-op's surface stop runs with the lock free."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events

        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            status=SessionStatus.COMPLETED,
            surface_ref="deadbeef",
        )
        daemon = LockProbeDaemon()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,
            force=True,
            native_daemon=daemon,
        )

        assert daemon.probes == [("free", {sess.id: SessionStatus.COMPLETED})]
        assert daemon.stop_calls == ["deadbeef"]
        events = read_events(
            consumer="_test_force_noop_lock_free",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert events == []

    def test_main_path_stop_runs_with_no_lock_held(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Regression pin: the normal completion path stops after the lock."""
        from cw.cli import _spawn_complete_impl

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        daemon = LockProbeDaemon()

        _spawn_complete_impl(
            session_id=sess.id,
            status="shipped",
            ticket_id=None,
            force=False,
            native_daemon=daemon,
        )

        assert daemon.probes == [("free", {sess.id: SessionStatus.COMPLETED})]

    @pytest.mark.parametrize("status_value", list(get_args(Status)))
    def test_status_routing_each_enum_value(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        status_value: str,
    ) -> None:
        """Each --status value is stored verbatim in the event payload."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id=f"status-{status_value[:8]}",
            name=f"test-client/auto-dev/STATUS-{status_value[:8]}",
        )
        daemon = FakeNativeDaemonClient()

        _spawn_complete_impl(
            session_id=sess.id,
            status=status_value,
            ticket_id=None,
            force=False,
            native_daemon=daemon,
        )

        events = read_events(
            consumer=f"_test_status_{status_value}",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        matching = [e for e in events if e.payload.get("session_id") == sess.id]
        assert len(matching) == 1
        assert matching[0].payload["status"] == status_value

    def test_already_completed_queue_task_raises(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """COMPLETED queue task + ACTIVE session → CwError."""
        from cw.cli import _spawn_complete_impl
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        # Seed a COMPLETED (not RUNNING) task
        task = TicketTask(
            ticket_id="GEN-42",
            client="test-client",
            status=QueueItemStatus.COMPLETED,
            session_id=sess.id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        daemon = FakeNativeDaemonClient()

        with pytest.raises(CwError):
            _spawn_complete_impl(
                session_id=sess.id,
                status="shipped",
                ticket_id=None,
                force=False,
                native_daemon=daemon,
            )

    def test_cli_spawn_complete_missing_session_exits_error(
        self, tmp_config_dir: Path
    ) -> None:
        """CLI: cw spawn complete nonexistent-id → non-zero exit, id in output."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["spawn", "complete", "nonexistent-id", "--status", "shipped"]
        )
        assert result.exit_code != 0
        assert "nonexistent-id" in result.output

    def test_regression_spawn_close_unaffected(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close still works unchanged after _seed_daemon_session extraction."""
        from cw.cli import _spawn_close_impl

        sess = _seed_daemon_session(tmp_path, tmp_config_dir)
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        state = load_state()
        closed = state.find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER


# ---------------------------------------------------------------------------
# CLI integration tests via Click CliRunner
# ---------------------------------------------------------------------------


class TestSpawnCLI:
    """CLI-layer tests using CliRunner."""

    def test_spawn_create_missing_client_shows_error(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """cw spawn --client unknown: exits with error about unknown client."""
        runner = CliRunner()
        prompt_file = _make_prompt_file(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        result = runner.invoke(
            main,
            [
                "spawn",
                "--client",
                "no-such-client",
                "--worktree",
                str(worktree),
                "--prompt-file",
                str(prompt_file),
            ],
        )

        assert result.exit_code != 0
        assert "no-such-client" in result.output

    def test_spawn_close_missing_session_shows_error(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """cw spawn close nonexistent: exits with error about missing session."""
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "nonexistent-id"])

        assert result.exit_code != 0
        assert "nonexistent-id" in result.output

    def test_spawn_close_ambiguous_name_lists_candidates(
        self, tmp_config_dir: Path
    ) -> None:
        """#2237: a shared name refuses and lists both ids instead of guessing."""
        name = "client-a/auto-dev/2212"
        save_state(
            CwState(
                sessions=[
                    _make_daemon_session(
                        id="done0001", name=name, status=SessionStatus.COMPLETED
                    ),
                    _make_daemon_session(id="live0002", name=name, surface_ref=None),
                ]
            )
        )

        result = CliRunner().invoke(main, ["spawn", "close", name])

        assert result.exit_code != 0
        assert "done0001" in result.output
        assert "live0002" in result.output
        assert "pass an id to choose" in result.output
        assert all(
            s.status != SessionStatus.COMPLETED or s.id == "done0001"
            for s in load_state().sessions
        )

    def test_cli_confirmed_dead_flag_accepted(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw spawn close --confirmed-dead <id>: flag accepted, session closed."""
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "--confirmed-dead", sess.id])

        assert result.exit_code == 0
        assert "Closed session" in result.output

    def test_cli_confirmed_dead_flag_defaults_off(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw spawn close <id> (no flag): optional, backwards-compatible."""
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", sess.id])

        assert result.exit_code == 0

    def test_cli_confirmed_dead_flag_trailing_position_accepted(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw spawn close <id> --confirmed-dead: Click accepts either order."""
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", sess.id, "--confirmed-dead"])

        assert result.exit_code == 0

    def test_cli_already_completed_exits_clean_and_stops_surface(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2480: `cw spawn close` on an already-completed session exits 0
        (not an error) and stops its lingering daemon surface."""
        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            status=SessionStatus.COMPLETED,
            surface_ref="deadbeef",
        )
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.spawn.get_native_daemon_client", lambda: daemon)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", sess.id])

        assert result.exit_code == 0, result.output
        assert "Closed session" in result.output
        assert daemon.stop_calls == ["deadbeef"]

    def test_spawn_post_launch_failure_surfaces_as_cli_error(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
        fail_state_write_after_launch: None,
    ) -> None:
        """(f) #2502: WorkerLaunchedError is a CwError, so handle_errors turns
        it into an ``Error:`` line naming the live session and short id."""
        events = capture_events(
            "cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        _write_test_client_yaml(tmp_config_dir, tmp_path)
        monkeypatch.setattr(
            "cw.spawn.get_native_daemon_client", lambda: mock_native_daemon
        )
        worktree = make_git_repo("wt-2502-cli")

        result = CliRunner().invoke(
            main,
            [
                "spawn",
                "--client",
                "test-client",
                "--worktree",
                str(worktree),
                "--prompt-file",
                str(_make_prompt_file(tmp_path)),
            ],
        )

        assert result.exit_code != 0
        assert result.output.startswith("Error:")
        assert post_launch_attention_payload(events)["session_id"] in result.output
        assert "00000001" in result.output
        assert mock_native_daemon.stop_calls == []


class TestSpawnCloseRequeue:
    """Tests for `cw spawn close --requeue` (#1889)."""

    def test_requeue_flag_cancels_then_requeues_to_pending(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """(a) RUNNING session closed with --requeue: task lands PENDING at its
        original stage, CLI output names both the close and the requeue."""
        from cw.dev_queue import load_dev_queue
        from cw.models import QueueItemStatus, Stage

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code == 0, result.output
        assert "Closed session" in result.output
        assert "Requeued GEN-42" in result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.PENDING
        assert task.stage == Stage.PLAN  # DEFAULT_STAGE — unchanged, no --stage

    def test_requeue_flag_no_resolvable_ticket_id_is_graceful_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """(b) session name with no auto-dev/ prefix: --requeue no-ops with an
        explanatory message, exit code still 0."""
        sess = _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            surface_ref=None,
            name="test-client/interactive-session",
        )
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code == 0
        assert "Closed session" in result.output
        assert "no-op" in result.output.lower()

    def test_requeue_flag_omitted_preserves_current_behavior(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """(c) --requeue omitted: no dev-queue mutation beyond the existing
        cancel_task_for_session, no TICKET_REQUEUED event."""
        from cw.dev_queue import load_dev_queue
        from cw.events import read_events
        from cw.models import OrchestratorEventType, QueueItemStatus

        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", sess.id])

        assert result.exit_code == 0
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED

        events = read_events(
            consumer="_test_requeue_omitted",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert events == []

    def test_requeue_flag_concierge_race_resolved_gracefully(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(d) the row is already advanced to PENDING by the concierge
        cancelled_row_restore recipe in the window between _spawn_close_impl's
        cancel and the --requeue call: exits 0, prints the "already recovered"
        message, does not raise RequeueStateError (uncaught) or double-emit
        TICKET_REQUEUED.

        Simulates the race directly via transition_task_status on the
        freshly-cancelled row (a focused unit test of the catch/fresh-read
        path per the plan), not a full concierge-tick integration test. The
        wrapper injects the race then delegates to the real requeue_ticket,
        so the RequeueStateError it raises is genuine -- caused by real state
        (row is PENDING), not fabricated -- and spawn_close's own catch/
        fresh-read logic is what's under test.
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
        from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket
        from cw.events import read_events
        from cw.models import OrchestratorEventType, QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)

        def _requeue_with_race(
            *args: object, **kwargs: object
        ) -> dict[str, str | bool | int]:
            store = load_dev_queue()
            task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
            transition_task_status(task, QueueItemStatus.PENDING)
            save_dev_queue(store)
            return real_requeue_ticket(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("cw.cli.spawn.requeue_ticket", _requeue_with_race)

        runner = CliRunner()
        result = runner.invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code == 0, result.output
        assert "already" in result.output.lower()

        events = read_events(
            consumer="_test_requeue_race",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert events == []

    def test_requeue_flag_emits_ticket_requeued_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """(e) TICKET_REQUEUED recorded with reason="spawn_close_requeue" and
        from_stage/to_stage sourced from requeue_ticket's return dict."""
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code == 0, result.output
        events = read_events(
            consumer="_test_requeue_event",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["ticket_id"] == "GEN-42"
        assert payload["client"] == "test-client"
        assert payload["reason"] == "spawn_close_requeue"
        assert payload["from_stage"] == "plan"
        assert payload["to_stage"] == "plan"

    def test_requeue_help_text_present(self, tmp_config_dir: Path) -> None:
        """(f) the literal --requeue help text is present in
        `cw spawn close --help` output."""
        runner = CliRunner()

        result = runner.invoke(main, ["spawn", "close", "--help"])

        assert result.exit_code == 0
        assert "--requeue" in result.output
        assert "cancelled_row_restore" in result.output

    def test_requeue_flag_genuine_state_error_propagates(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(g) the row lands on a genuinely non-approvable status (FAILED, not
        PENDING/RUNNING) in the window between _spawn_close_impl's cancel and
        the --requeue call: this is NOT the concierge race (scenario d) --
        the except RequeueStateError handler's fresh read finds a real state
        problem, so the bare `raise` fires and the CLI exits non-zero instead
        of silently no-op'ing.

        Mirrors scenario (d)'s realism: the wrapper injects the race then
        delegates to the real requeue_ticket, so the RequeueStateError it
        raises is genuine, not fabricated.
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
        from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket
        from cw.events import read_events
        from cw.models import OrchestratorEventType, QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(tmp_path, tmp_config_dir, surface_ref=None)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)

        def _requeue_with_genuine_state_error(
            *args: object, **kwargs: object
        ) -> dict[str, str | bool | int]:
            store = load_dev_queue()
            task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
            transition_task_status(task, QueueItemStatus.FAILED)
            save_dev_queue(store)
            return real_requeue_ticket(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "cw.cli.spawn.requeue_ticket", _requeue_with_genuine_state_error
        )

        runner = CliRunner()
        result = runner.invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code != 0
        assert "GEN-42" in result.output
        assert "no-op" not in result.output.lower()

        events = read_events(
            consumer="_test_requeue_genuine_error",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert events == []

    # 8-char hex so the #2275 live-session guard treats them as daemon surfaces.
    _CLOSED_REF = "aaaa1111"
    _OTHER_REF = "bbbb2222"

    def _patch_daemons(
        self, monkeypatch: pytest.MonkeyPatch, *live_refs: str
    ) -> FakeNativeDaemonClient:
        """Close-side stop goes to a throwaway fake; the requeue guard reads a
        roster fake that still lists *live_refs* (roster not caught up yet)."""
        roster = FakeNativeDaemonClient()
        roster._live = set(live_refs)
        monkeypatch.setattr(
            "cw.cli.spawn.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.dev_queue.requeue.get_native_daemon_client", lambda: roster
        )
        return roster

    def test_requeue_flag_ignores_the_just_closed_session_even_if_still_in_roster(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2275: the session --requeue just closed is excluded from the
        live-session guard via ignore_session_ids, regardless of roster lag."""
        from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, surface_ref=self._CLOSED_REF
        )
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        self._patch_daemons(monkeypatch, self._CLOSED_REF)
        seen_kwargs: list[dict[str, Any]] = []

        def _spy(*args: Any, **kwargs: Any) -> dict[str, str | bool | int]:
            seen_kwargs.append(kwargs)
            return real_requeue_ticket(*args, **kwargs)

        monkeypatch.setattr("cw.cli.spawn.requeue_ticket", _spy)

        result = CliRunner().invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code == 0, result.output
        assert "Requeued GEN-42" in result.output
        assert seen_kwargs[0]["ignore_session_ids"] == frozenset({sess.id})

    def test_requeue_flag_still_refused_by_a_second_different_live_session(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2275: only the just-closed session is exempt -- a second live
        session for the same ticket still refuses the requeue."""
        from cw.dev_queue import load_dev_queue
        from cw.models import QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, surface_ref=self._CLOSED_REF
        )
        other = _make_daemon_session(
            id="othr5678",
            name="test-client/auto-dev/GEN-42",
            client="test-client",
            surface_ref=self._OTHER_REF,
        )
        state = load_state()
        state.sessions.append(other)
        save_state(state)
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        self._patch_daemons(monkeypatch, self._CLOSED_REF, self._OTHER_REF)

        result = CliRunner().invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code != 0
        assert other.id in result.output
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED

    def test_requeue_flag_refused_when_roster_unreadable(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2275 review round 1: the just-closed exemption covers one known
        session, not the unknown rest an unreadable roster cannot rule out --
        the close lands, the requeue is refused with the fail-closed message."""
        from cw.dev_queue import load_dev_queue
        from cw.events import read_events
        from cw.models import OrchestratorEventType, QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, surface_ref=self._CLOSED_REF
        )
        _seed_running_task(ticket_id="GEN-42", client="test-client", session_id=sess.id)
        roster = self._patch_daemons(monkeypatch)
        roster.roster_unreadable = True

        result = CliRunner().invoke(main, ["spawn", "close", "--requeue", sess.id])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert (
            f"daemon roster unreadable at {roster.roster_path};"
            " cannot rule out a live session for #GEN-42"
        ) in output
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "GEN-42")
        assert task.status == QueueItemStatus.CANCELLED
        requeued = read_events(
            consumer="_test_requeue_roster_unreadable",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert requeued == []


class TestSpawnCloseRequeueImplDirect:
    """Direct-call unit tests for _spawn_close_requeue_impl (#1889).

    Companions to TestSpawnCloseRequeue's 7 CliRunner-based scenarios, which
    cover the `--requeue` flag's CLI wiring. These call the function
    directly -- the purpose stated in its docstring ("Separated from the
    Click command so tests can call it directly") -- mirroring the
    direct-call pattern used for its siblings in TestSpawnClose /
    TestSpawnComplete.
    """

    def test_noop_when_ticket_id_or_client_none(
        self, tmp_config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Guard clause: ticket_id=None or client=None each short-circuit to
        a no-op message without calling requeue_ticket."""
        from cw.cli import _spawn_close_requeue_impl

        _spawn_close_requeue_impl(
            session_id="dead1234", ticket_id=None, client="test-client"
        )
        out = capsys.readouterr().out
        assert "no-op" in out.lower()

        _spawn_close_requeue_impl(
            session_id="dead1234", ticket_id="GEN-42", client=None
        )
        out = capsys.readouterr().out
        assert "no-op" in out.lower()

    def test_race_resolved_noop_when_fresh_read_finds_pending_or_running(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RequeueStateError whose fresh read finds PENDING/RUNNING is the
        concierge race (#1889): swallowed as a no-op, not raised.

        Mirrors TestSpawnCloseRequeue's scenario (d): the wrapper injects
        the race then delegates to the real requeue_ticket, so the
        RequeueStateError raised is genuine, not fabricated.
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
        from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket
        from cw.events import read_events
        from cw.models import OrchestratorEventType, QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        _seed_running_task(ticket_id="GEN-42", client="test-client")

        def _requeue_with_race(
            *args: object, **kwargs: object
        ) -> dict[str, str | bool | int]:
            store = load_dev_queue()
            task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
            transition_task_status(task, QueueItemStatus.PENDING)
            save_dev_queue(store)
            return real_requeue_ticket(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("cw.cli.spawn.requeue_ticket", _requeue_with_race)

        from cw.cli import _spawn_close_requeue_impl

        _spawn_close_requeue_impl(
            session_id="dead1234", ticket_id="GEN-42", client="test-client"
        )

        events = read_events(
            consumer="_test_requeue_impl_direct_race",
            event_types=[OrchestratorEventType.TICKET_REQUEUED],
        )
        assert events == []

    def test_genuine_state_error_propagates(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RequeueStateError whose fresh read finds neither PENDING nor
        RUNNING (e.g. FAILED) is a genuine state problem: the bare `raise`
        fires and propagates out of the function.

        Mirrors TestSpawnCloseRequeue's scenario (g).
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
        from cw.dev_queue.requeue import requeue_ticket as real_requeue_ticket
        from cw.exceptions import RequeueStateError
        from cw.models import QueueItemStatus

        _write_test_client_yaml(tmp_config_dir, tmp_path)
        _seed_running_task(ticket_id="GEN-42", client="test-client")

        def _requeue_with_genuine_state_error(
            *args: object, **kwargs: object
        ) -> dict[str, str | bool | int]:
            store = load_dev_queue()
            task = next(t for t in store.tasks if t.ticket_id == "GEN-42")
            transition_task_status(task, QueueItemStatus.FAILED)
            save_dev_queue(store)
            return real_requeue_ticket(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "cw.cli.spawn.requeue_ticket", _requeue_with_genuine_state_error
        )

        from cw.cli import _spawn_close_requeue_impl

        with pytest.raises(RequeueStateError):
            _spawn_close_requeue_impl(
                session_id="dead1234", ticket_id="GEN-42", client="test-client"
            )


# ---------------------------------------------------------------------------
# Tests for #314 task fields in cw-context.json
# ---------------------------------------------------------------------------


def _make_pending_task(
    ticket_id: str = "GEN-314",
    client: str = "test-client",
    attempts: int = 0,
    scope_hint: str | None = "large",
    plan_source: str | None = None,
    headless_timeout_override: int | None = None,
) -> TicketTask:
    from cw.models import QueueItemStatus

    return _make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.PENDING,
        attempts=attempts,
        scope_hint=scope_hint,
        plan_source=plan_source,
        headless_timeout_override=headless_timeout_override,
    )


class TestWriteHookContextTaskFields:
    """Tests for #314: task fields written into cw-context.json by spawn_create_impl."""

    def test_task_none_omits_task_fields(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Backward compat: task=None → context has no task-specific fields."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-314-task-none")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            ticket_id="GEN-314",
            headless=True,
            task=None,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        for field in (
            "attempt",
            "wall_clock_budget_seconds",
            "stage_started_at",
            "expected_sentinel_schema_ref",
            "queue_metadata",
            "world_state_snapshot",
        ):
            assert field not in context, f"unexpected field {field!r} when task=None"

    def test_task_nonnone_writes_all_task_fields(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """task provided → all #314 context fields are written with correct values."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-314-task-fields")
        task = _make_pending_task(attempts=2, scope_hint="large")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            ticket_id="GEN-314",
            headless=True,
            task=task,
            wall_clock_budget_seconds=5400,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["attempt"] == 2
        assert context["wall_clock_budget_seconds"] == 5400
        assert "stage_started_at" in context
        ref = context["expected_sentinel_schema_ref"]
        assert ref["model"] == "AutoDevResult"
        assert ref["version"] == AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION
        assert "cw schema show" in ref["command"]
        qm = context["queue_metadata"]
        assert qm["scope_hint"] == "large"
        assert qm["plan_source"] is None
        assert qm["headless_timeout_override"] is None
        assert qm["regressed_into_stage"] is None
        assert qm["plan_approved_at"] is None
        ws = context["world_state_snapshot"]
        assert ws["origin_main_branch"] == "main"
        assert ws["prior_attempts_summary"] == []

    def test_regressed_into_stage_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#1794: a task regressed into IMPL must carry that per-arrival signal
        into the worker's queue_metadata, where auto-dev-impl.md's Pre-Stage
        Detector Guard reads it."""
        from cw.models import Stage
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-1794-regress")
        task = _make_pending_task()
        task.regressed_into_stage = Stage.IMPL

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-impl GEN-1794 --headless",
            label="auto-dev/GEN-1794",
            native_daemon=daemon,
            ticket_id="GEN-1794",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        # Stage is a StrEnum, so json.dumps renders the plain stage value.
        assert context["queue_metadata"]["regressed_into_stage"] == "impl"

    def test_pending_operator_comment_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#1730: the pending-send-back marker must reach the worker's
        queue_metadata, where auto-dev-review.md and codex_review read it."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-1730-marker")
        task = _make_pending_task()
        task.pending_operator_comment = True

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-review GEN-1730 --headless",
            label="auto-dev/GEN-1730",
            native_daemon=daemon,
            ticket_id="GEN-1730",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["queue_metadata"]["pending_operator_comment"] is True

    def test_plan_approved_at_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """The tracker-neutral plan-approval record (dev-queue v35) reaches
        the worker as an ISO-8601 timestamp, where auto-dev-plan.md's
        Checkpoint 1 reads it as approval evidence."""
        from datetime import UTC, datetime

        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-plan-approved")
        task = _make_pending_task()
        task.plan_approved_at = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-plan GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            ticket_id="GEN-314",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert (
            context["queue_metadata"]["plan_approved_at"] == "2026-09-04T12:30:00+00:00"
        )

    def test_plan_approved_fingerprint_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """The draft fingerprint the approval was bound to (dev-queue v36)
        reaches the worker verbatim, where Checkpoint 1 compares it against the
        draft it is about to auto-skip (#2102)."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-plan-fingerprint")
        task = _make_pending_task()
        task.plan_approved_fingerprint = "abc123"

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-plan GEN-2102 --headless",
            label="auto-dev/GEN-2102",
            native_daemon=daemon,
            ticket_id="GEN-2102",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["queue_metadata"]["plan_approved_fingerprint"] == "abc123"

    def test_plan_approved_fingerprint_null_threaded_as_null(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """An unstamped row threads an explicit null, not a missing key — the
        consumer distinguishes "no approval bound" from "key absent"."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-plan-fingerprint-null")
        task = _make_pending_task()

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-plan GEN-2102 --headless",
            label="auto-dev/GEN-2102",
            native_daemon=daemon,
            ticket_id="GEN-2102",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert "plan_approved_fingerprint" in context["queue_metadata"]
        assert context["queue_metadata"]["plan_approved_fingerprint"] is None

    def test_scope_drift_approval_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """The operator's plan_scope_drift approval (dev-queue v40, #2337)
        reaches the worker verbatim, where Step 2.5 gate 2 feeds the files to
        the scope-conformance script once the head passes the ancestry check."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-scope-drift-approval")
        task = _make_pending_task()
        task.scope_drift_approved_extra_files = ["src/a.py", "tests/test_a.py"]
        task.scope_drift_approved_head = "0123abcd" * 5

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-impl GEN-2337 --headless",
            label="auto-dev/GEN-2337",
            native_daemon=daemon,
            ticket_id="GEN-2337",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        metadata = context["queue_metadata"]
        assert metadata[SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY] == [
            "src/a.py",
            "tests/test_a.py",
        ]
        assert metadata[SCOPE_DRIFT_APPROVED_HEAD_KEY] == "0123abcd" * 5

    def test_scope_drift_approval_null_threaded_as_null(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """An unapproved row threads explicit nulls, not missing keys."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-scope-drift-approval-null")
        task = _make_pending_task()

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-impl GEN-2337 --headless",
            label="auto-dev/GEN-2337",
            native_daemon=daemon,
            ticket_id="GEN-2337",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        metadata = context["queue_metadata"]
        assert SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY in metadata
        assert metadata[SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY] is None
        assert SCOPE_DRIFT_APPROVED_HEAD_KEY in metadata
        assert metadata[SCOPE_DRIFT_APPROVED_HEAD_KEY] is None

    def test_must_fix_override_threaded_into_queue_metadata(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """The operator's MUST_FIX override (dev-queue v42, #2205) reaches the
        FINALIZE worker verbatim, where check_must_fix_override.py compares it
        against the live verdict -- blocked_reason has been cleared by then."""
        from cw.spawn import spawn_create_impl

        assert MUST_FIX_OVERRIDE_KEY in TicketTask.model_fields
        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-must-fix-override")
        task = _make_pending_task()
        task.must_fix_override = MustFixOverride(
            actor="octocat",
            reason="ship it",
            reviewed_sha="0123abcd" * 5,
            finding_ids=[("src/a.py", "bug here")],
            recorded_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        )

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-finalize GEN-2205 --headless",
            label="auto-dev/GEN-2205",
            native_daemon=daemon,
            ticket_id="GEN-2205",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["queue_metadata"][MUST_FIX_OVERRIDE_KEY] == {
            "actor": "octocat",
            "reason": "ship it",
            "reviewed_sha": "0123abcd" * 5,
            "finding_ids": [["src/a.py", "bug here"]],
            "recorded_at": "2026-09-25T12:00:00Z",
        }

    def test_must_fix_override_null_threaded_as_null(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """An unstamped row threads an explicit null, not a missing key."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-must-fix-override-null")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev-finalize GEN-2205 --headless",
            label="auto-dev/GEN-2205",
            native_daemon=daemon,
            ticket_id="GEN-2205",
            headless=True,
            task=_make_pending_task(),
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert MUST_FIX_OVERRIDE_KEY in context["queue_metadata"]
        assert context["queue_metadata"][MUST_FIX_OVERRIDE_KEY] is None

    def test_git_failure_sets_origin_sha_null(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """No remote → git rev-parse fails → origin_main_sha_at_spawn is null."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        # make_git_repo creates a repo with no remote; rev-parse origin/main fails.
        worktree = make_git_repo("wt-314-git-fail")
        task = _make_pending_task(scope_hint=None)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            ticket_id="GEN-314",
            headless=True,
            task=task,
            wall_clock_budget_seconds=3600,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["world_state_snapshot"]["origin_main_sha_at_spawn"] is None

    def test_attempt_from_task_attempts_not_incremented(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """context['attempt'] == task.attempts exactly — not task.attempts + 1."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-314-attempts")
        task = _make_pending_task(attempts=3)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            task=task,
            wall_clock_budget_seconds=0,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["attempt"] == 3

    def test_wall_clock_budget_passthrough(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """wall_clock_budget_seconds passed to spawn_create_impl lands in context."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-314-budget")
        task = _make_pending_task()

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            task=task,
            wall_clock_budget_seconds=7200,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["wall_clock_budget_seconds"] == 7200


class TestWriteHookContextOriginShaSuccess:
    """Tests for the git-success path in _write_hook_context (#314)."""

    def test_origin_sha_populated_when_git_succeeds(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When git rev-parse succeeds, origin_main_sha_at_spawn is the SHA."""
        import subprocess as subprocess_mod

        from cw.spawn import spawn_create_impl

        fake_sha = "abc1234def5678"
        real_run = subprocess_mod.run

        def patched_run(
            cmd: list[str], **kwargs: Any
        ) -> subprocess_mod.CompletedProcess[Any]:
            if "rev-parse" in cmd and "origin/main" in cmd:
                return subprocess_mod.CompletedProcess(
                    cmd, 0, stdout=fake_sha + "\n", stderr=""
                )
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", patched_run)

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-sha-success")
        task = _make_pending_task()

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-314 --headless",
            label="auto-dev/GEN-314",
            native_daemon=daemon,
            task=task,
            wall_clock_budget_seconds=5400,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["world_state_snapshot"]["origin_main_sha_at_spawn"] == fake_sha


def test_spawn_create_impl_orchestrate_purpose(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn_create_impl(purpose=ORCHESTRATE) stamps session.purpose and context."""
    from cw.spawn import spawn_create_impl

    client = _make_client(tmp_path)
    daemon = FakeNativeDaemonClient()
    worktree = make_git_repo("worktree-orchestrate-purpose")

    session_id = spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt="You are the orchestrate session.",
        label="orchestrate/impl",
        native_daemon=daemon,
        purpose=SessionPurpose.ORCHESTRATE,
    )

    state = load_state()
    sess = state.find_by_name_or_id(session_id)
    assert sess is not None
    assert sess.purpose == SessionPurpose.ORCHESTRATE

    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    assert context["purpose"] == "orchestrate"


def test_spawn_create_impl_default_purpose(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn_create_impl default path stamps IMPL."""
    from cw.spawn import spawn_create_impl

    client = _make_client(tmp_path)
    daemon = FakeNativeDaemonClient()
    worktree = make_git_repo("worktree-default-purpose")

    session_id = spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt="Do the thing.",
        label=None,
        native_daemon=daemon,
    )

    state = load_state()
    sess = state.find_by_name_or_id(session_id)
    assert sess is not None
    assert sess.purpose == SessionPurpose.IMPL

    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    assert context["purpose"] == "impl"


# ---------------------------------------------------------------------------
# Tests for #766 — workspace_path in cw-context.json (forbidden main-checkout)
# ---------------------------------------------------------------------------


class TestCwContextWorkspacePath:
    """Tests for the workspace_path field added to cw-context.json (#766).

    The field carries the operator's main checkout path — the FORBIDDEN
    destination for any git mutation from a dispatch worker.  A guard script
    or PreToolUse hook reads it to block git commit/push when the resolved
    repo root matches this path.
    """

    def test_workspace_path_written_from_client(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """workspace_path written == client.workspace_path (resolved)."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="ws-client")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-766-workspace-path")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-766 --headless",
            label="auto-dev/GEN-766",
            native_daemon=daemon,
            ticket_id="GEN-766",
            headless=True,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert "workspace_path" in context
        assert context["workspace_path"] == str(client.workspace_path.resolve())

    def test_workspace_path_differs_from_worktree_path(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """workspace_path and worktree_path are distinct paths.

        This is the invariant the guard relies on: the worktree (allowed) is
        not the same directory as the workspace (forbidden).
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="guard-client")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-766-distinct-paths")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-766 --headless",
            label="auto-dev/GEN-766",
            native_daemon=daemon,
            ticket_id="GEN-766",
            headless=True,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["workspace_path"] != context["worktree_path"]

    def test_workspace_path_resolves_symlinks(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """workspace_path is resolved (symlinks canonicalized) for guard comparison."""
        from cw.models import SessionOrigin
        from cw.spawn import _write_hook_context

        real_ws = tmp_path / "real-workspace"
        real_ws.mkdir()
        link_ws = tmp_path / "link-workspace"
        link_ws.symlink_to(real_ws)

        worktree = make_git_repo("wt-766-symlink")

        _write_hook_context(
            worktree,
            session_id="abc",
            session_name="cli/sym",
            client="cli",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.DAEMON,
            workspace_path=link_ws,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        # Must resolve to the real path, not the symlink.
        assert context["workspace_path"] == str(real_ws.resolve())

    def test_workspace_path_null_when_not_provided(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """workspace_path is null when not passed to _write_hook_context.

        Backward-compat: USER-origin sessions that predate #766 may not carry
        this field.  The guard must handle null gracefully (skip the check).
        """
        from cw.models import SessionOrigin
        from cw.spawn import _write_hook_context

        worktree = make_git_repo("wt-766-null-ws")

        _write_hook_context(
            worktree,
            session_id="xyz",
            session_name="cli/noworkspace",
            client="cli",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.DAEMON,
            # workspace_path intentionally omitted
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert "workspace_path" in context
        assert context["workspace_path"] is None

    def test_schema_version_incremented_to_2(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """cw-context.json schema_version is current (11 after the
        merge_gate_ignore_paths addition)."""
        from cw.spawn import CW_CONTEXT_SCHEMA_VERSION, spawn_create_impl

        assert CW_CONTEXT_SCHEMA_VERSION == 11

        client = _make_client(tmp_path, name="schema-v2-client")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-766-schema-v2")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-766 --headless",
            label="auto-dev/GEN-766",
            native_daemon=daemon,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["schema_version"] == 11


class TestCwContextMergeGateIgnorePaths:
    """_write_hook_context stamps the #2431 merge_gate_ignore_paths key.

    auto-dev-finalize.md Step 4a's headless fence reads it with ``jq`` — the
    fence has no other route to the client's ClientConfig field.
    """

    def test_write_hook_context_writes_merge_gate_ignore_paths_default_empty_list(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "wt-mg-default"
        worktree.mkdir()
        _write_hook_context(
            worktree,
            session_id="s1",
            session_name="acme/impl",
            client="acme",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.USER,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["merge_gate_ignore_paths"] == []

    def test_write_hook_context_writes_merge_gate_ignore_paths_when_provided(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "wt-mg-set"
        worktree.mkdir()
        _write_hook_context(
            worktree,
            session_id="s1",
            session_name="acme/impl",
            client="acme",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.DAEMON,
            merge_gate_ignore_paths=["mypy-baseline.txt", "uv.lock"],
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["merge_gate_ignore_paths"] == ["mypy-baseline.txt", "uv.lock"]

    def test_spawn_create_impl_forwards_client_merge_gate_ignore_paths(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="mg-client").model_copy(
            update={"merge_gate_ignore_paths": ["mypy-baseline.txt"]}
        )
        worktree = make_git_repo("wt-2431-mg")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-2431 --headless",
            label="auto-dev/GEN-2431",
            native_daemon=FakeNativeDaemonClient(),
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["merge_gate_ignore_paths"] == ["mypy-baseline.txt"]


class TestCwContextLaneStamp:
    """_write_hook_context stamps the #1946 lane key the busy-wait guard reads."""

    def test_write_hook_context_stamps_lane(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """An explicit lane lands in cw-context.json verbatim."""
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "wt-lane"
        worktree.mkdir()
        _write_hook_context(
            worktree,
            session_id="s1",
            session_name="acme/impl",
            client="acme",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.DAEMON,
            lane="my-lane",
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["lane"] == "my-lane"

    def test_write_hook_context_lane_defaults_to_none(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The lane-less call shape (cw.session) still writes the key as null.

        The key is always present so a consumer never has to distinguish
        "no lane" from "context predates the schema-v6 addition".
        """
        from cw.spawn import _write_hook_context

        worktree = tmp_path / "wt-no-lane"
        worktree.mkdir()
        _write_hook_context(
            worktree,
            session_id="s1",
            session_name="acme/impl",
            client="acme",
            purpose="impl",
            ticket_id=None,
            origin=SessionOrigin.USER,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["lane"] is None

    def test_spawn_create_impl_forwards_its_lane(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_create_impl's in-scope lane reaches the written context."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="lane-stamp-client")
        worktree = make_git_repo("wt-1946-lane")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev GEN-1946 --headless",
            label="auto-dev/GEN-1946",
            native_daemon=FakeNativeDaemonClient(),
            lane="fast",
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["lane"] == "fast"


class TestAgentSpawnStampSeeding:
    """_write_hook_context seeds the #1646 unresolved-subagent-spawn stamp."""

    @pytest.mark.parametrize("origin", [SessionOrigin.DAEMON, SessionOrigin.USER])
    def test_write_hook_context_seeds_unresolved_spawn_counter_at_zero(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        origin: SessionOrigin,
    ) -> None:
        """Both origins get the stamp seeded to a resolved (zero) count.

        Parametrized over origin rather than over purpose/stage: both existing
        call sites (``spawn_create_impl``, ``session.resume_session``) flow
        through this one writer and differ only in origin, so origin is the
        axis that actually varies at the seam.
        """
        from cw.models import (
            AGENT_SPAWN_LAST_STAMPED_AT_KEY,
            AGENT_SPAWN_STAMP_KEY,
            AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
        )
        from cw.spawn import _write_hook_context

        worktree = tmp_path / f"wt-1646-{origin.value}"
        worktree.mkdir()

        _write_hook_context(
            worktree,
            session_id="sess1646",
            session_name="client-a/impl",
            client="client-a",
            purpose="impl",
            ticket_id="1646",
            origin=origin,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        stamp = context[AGENT_SPAWN_STAMP_KEY]
        assert stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY] == 0
        assert stamp[AGENT_SPAWN_LAST_STAMPED_AT_KEY] is None


class TestHookMechanismIsPurposeAndStageAgnostic:
    """#1646: the stamp mechanism must key only off worktree_path/cw-context.

    Structural rather than parametrized on purpose. ``SessionPurpose`` has no
    correspondence to the four pipeline stages (those live in the separate
    ``Stage`` enum), and every stage-dispatch call site passes
    ``purpose=SessionPurpose.IMPL`` unconditionally — so a parametrized
    round-trip would only prove that a string survives a dict assignment. What
    can actually regress is somebody later adding an ``if purpose == ...``
    branch to one of these three functions, and that is what this asserts
    against.
    """

    def test_hook_mechanism_has_no_purpose_or_stage_conditionals(self) -> None:
        """None of the three mechanism seams branch on purpose or stage."""
        import inspect
        import re

        from cw.reconcile._shared import _read_unresolved_subagent_spawn
        from cw.spawn import _write_hook_context

        forbidden = re.compile(
            r"(if|elif|match)\b[^\n]*\b(purpose|SessionPurpose|stage|Stage)\b"
        )
        for func in (_write_hook_context, _read_unresolved_subagent_spawn):
            source = inspect.getsource(func)
            assert not forbidden.search(source), (
                f"{func.__name__} gained a purpose/stage conditional — the #1646 "
                "stamp mechanism must key only off worktree_path/cw-context.json"
            )

    def test_hook_settings_template_has_no_purpose_or_stage_keys(self) -> None:
        """The settings template is a constant, not a per-purpose computation."""
        from cw.spawn import _build_hook_settings

        rendered = json.dumps(_build_hook_settings(_FAKE_CONTEXT_PATH))
        assert "purpose" not in rendered
        assert "stage" not in rendered


class TestSpawnCreateImplCsidBackfill:
    """Tests for claude_session_id backfill at spawn-return (issue #635)."""

    def test_csid_backfill_when_transcript_present(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """spawn_create_impl backfills claude_session_id when transcript is found."""
        import cw.spawn as spawn_mod
        from cw.spawn import spawn_create_impl

        monkeypatch.setattr(
            spawn_mod, "_csid_from_transcript", lambda _: "abc12345def67890"
        )

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-csid-present")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Fix #635",
            label=None,
            native_daemon=daemon,
        )

        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        assert sess is not None
        assert sess.claude_session_id == "abc12345def67890"

    def test_csid_backfill_when_transcript_absent(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """spawn_create_impl leaves claude_session_id None when transcript is absent."""
        import cw.spawn as spawn_mod
        from cw.spawn import spawn_create_impl

        monkeypatch.setattr(spawn_mod, "_csid_from_transcript", lambda _: None)

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("worktree-csid-absent")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="Fix #635",
            label=None,
            native_daemon=daemon,
        )

        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        assert sess is not None
        assert sess.claude_session_id is None


# ---------------------------------------------------------------------------
# Tests for config-driven --disallowed-tools injection (replaces the #726
# hard-coded, tracker-gated Linear MCP disallow). Tracker no longer affects
# the disallow at all; the source of truth is
# ``OrchestratorConfig.disallowed_mcp_tools``, plumbed through
# ``build_disallowed_tools_arg``. The single `=`-joined token shape (#733 —
# NOT the two-token ``["--disallowed-tools", pattern]`` form, whose variadic
# flag would swallow the positional prompt) is preserved.
# ---------------------------------------------------------------------------


def _write_orchestrator_disallow(patterns: list[str]) -> None:
    """Write ``disallowed_mcp_tools: [...]`` to the orchestrator config file."""
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"  - {json.dumps(p)}\n" for p in patterns)
    path.write_text(f"disallowed_mcp_tools:\n{lines}", encoding="utf-8")


class TestBuildDisallowedToolsArg:
    """Pure-function unit tests for ``build_disallowed_tools_arg``."""

    def test_empty_patterns_returns_empty_list(self) -> None:
        assert build_disallowed_tools_arg([]) == []

    def test_single_pattern_returns_single_equals_joined_token(self) -> None:
        result = build_disallowed_tools_arg(["mcp__plugin_linear_linear__*"])
        assert result == ["--disallowed-tools=mcp__plugin_linear_linear__*"]

    def test_multiple_patterns_are_comma_joined_into_one_token(self) -> None:
        result = build_disallowed_tools_arg(["a", "Bash(git *)"])
        assert result == ["--disallowed-tools=a,Bash(git *)"]

    def test_non_empty_result_is_always_a_single_token(self) -> None:
        # Never the two-token variadic-swallowing form (#733).
        result = build_disallowed_tools_arg(["mcp__plugin_linear_linear__*"])
        assert len(result) == 1
        assert result[0].startswith("--disallowed-tools=")


class TestDisallowedMcpTools:
    """Spawn-integration tests: ``OrchestratorConfig.disallowed_mcp_tools`` is
    injected into ``claude --bg`` extra_args via ``build_disallowed_tools_arg``.
    """

    def test_no_config_injects_no_disallow(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """No orchestrator config written → defaults to [] → no disallow flag."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-disallow-no-config")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] is None

    def test_single_pattern_injects_single_token(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """One configured pattern → one `=`-joined --disallowed-tools token."""
        from cw.spawn import spawn_create_impl

        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-disallow-single")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--disallowed-tools=mcp__plugin_linear_linear__*"
        ]

    def test_multiple_patterns_comma_joined_single_token(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Multiple configured patterns → one comma-joined token, not several."""
        from cw.spawn import spawn_create_impl

        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*", "mcp__foo__*"])
        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-disallow-multiple")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--disallowed-tools=mcp__plugin_linear_linear__*,mcp__foo__*"
        ]
        # #733 guard: always one token, never split across multiple flags.
        assert len(daemon.spawn_extra_args[0]) == 1

    def test_worker_model_ordered_before_disallow(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """worker_model + configured disallow: --model first, then disallow."""
        from cw.spawn import spawn_create_impl

        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        workspace = tmp_path / "workspace" / "model-client"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="model-client",
            workspace_path=workspace,
            worker_model="claude-sonnet-4-6-20251015",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-disallow-model")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--model",
            "claude-sonnet-4-6-20251015",
            "--disallowed-tools=mcp__plugin_linear_linear__*",
        ]

    def test_extra_args_appended_after_disallow(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Caller extra_args append after --disallowed-tools."""
        from cw.spawn import spawn_create_impl

        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-disallow-extra-args")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
            extra_args=["--resume", "abc12345"],
        )

        assert daemon.spawn_extra_args[0] == [
            "--disallowed-tools=mcp__plugin_linear_linear__*",
            "--resume",
            "abc12345",
        ]


# ---------------------------------------------------------------------------
# Tests for #736: prompt survives as trailing positional in assembled argv
# ---------------------------------------------------------------------------


class TestSpawnArgvPromptPositional:
    """Regression guard for #733: the worker prompt must be the final token in
    the assembled ``claude --bg`` argv.

    Bug: ``--disallowed-tools <pattern>`` (two-token, space-separated) let
    ``claude``'s variadic flag parser consume the prompt as an extra value,
    leaving the worker promptless.  Fix: use ``--disallowed-tools=<pattern>``
    (``=``-joined single token) which binds exactly one value.

    These tests verify the fully-assembled argv has the prompt last, covering
    both spawn chokepoints (spawn_create_impl and resume_session).
    """

    def test_spawn_create_impl_prompt_is_trailing_positional(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """spawn_create_impl: prompt is the final token of the assembled argv.

        Uses the maximally-loaded extra_args set: ``--model`` (worker_model)
        then ``--disallowed-tools=`` (configured disallow patterns).  This is
        the exact argv shape that triggered #733 when the disallow flag was in
        two-token form.
        """
        from cw.native_daemon import _DEFAULT_PERMISSION_MODE, _build_spawn_argv
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "gh-argv-create"
        workspace.mkdir(parents=True)
        _write_orchestrator_disallow(["mcp__plugin_linear_linear__*"])
        client = ClientConfig(
            name="gh-argv-create",
            workspace_path=workspace,
            worker_model="claude-sonnet-4-6-20251015",
        )
        prompt = "/auto-dev 733 --headless"
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-argv-spawn-create")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt=prompt,
            label="auto-dev-733",
            native_daemon=daemon,
        )

        _, received_prompt = daemon.spawn_calls[0]
        extra_args = daemon.spawn_extra_args[0]
        full_argv = _build_spawn_argv(
            mode=_DEFAULT_PERMISSION_MODE,
            extra_args=extra_args,
            prompt=received_prompt,
        )

        # Prompt must be the final argv token.
        assert full_argv[-1] == prompt
        # Extra sanity: the prompt reached spawn_bg unmodified.
        assert received_prompt == prompt


# ---------------------------------------------------------------------------
# Tests for #520: roster-registration verification after spawn_bg
# ---------------------------------------------------------------------------


class TestRosterRegistrationVerification:
    """Tests for spawn_create_impl's post-spawn roster verification (#520).

    After spawn_bg returns a short id, cw polls the daemon roster to confirm
    the supervisor actually adopted the worker. Silent spawn flakes — where
    the short id is returned but the worker never appears in roster.json —
    are caught here rather than 30 min later via the idle watchdog.
    """

    def test_happy_path_session_saved_when_worker_registered(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Normal spawn: worker in roster → session saved, no error."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-520-happy")

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 520 --headless",
            label="auto-dev-520",
            native_daemon=daemon,
        )

        state = load_state()
        assert len(state.sessions) == 1
        assert state.sessions[0].id == session_id

    def test_unregistered_worker_raises_spawn_unregistered_error(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Worker never appears in roster → SpawnUnregisteredError raised."""
        from cw.exceptions import SpawnUnregisteredError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True
        worktree = make_git_repo("wt-520-unregistered")

        with pytest.raises(SpawnUnregisteredError, match="spawn_unregistered"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 520 --headless",
                label="auto-dev-520",
                native_daemon=daemon,
                _roster_poll_timeout=0.0,
            )

    def test_unregistered_worker_does_not_save_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Worker never appears → session NOT saved to state (no phantom RUNNING)."""
        from cw.exceptions import SpawnUnregisteredError
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True
        worktree = make_git_repo("wt-520-no-phantom")

        with pytest.raises(SpawnUnregisteredError):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 520 --headless",
                label="auto-dev-520",
                native_daemon=daemon,
                _roster_poll_timeout=0.0,
            )

        state = load_state()
        assert state.sessions == []

    def test_unregistered_worker_emits_spawn_unregistered_event(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Worker never appears → SESSION_SPAWN_UNREGISTERED event in inbox."""
        from cw.events import read_events
        from cw.exceptions import SpawnUnregisteredError
        from cw.models import OrchestratorEventType
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True
        worktree = make_git_repo("wt-520-event")

        with pytest.raises(SpawnUnregisteredError):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 520 --headless",
                label="auto-dev-520",
                native_daemon=daemon,
                ticket_id="520",
                _roster_poll_timeout=0.0,
            )

        events = read_events(
            consumer="_test_520_event",
            event_types=[OrchestratorEventType.SESSION_SPAWN_UNREGISTERED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["reason"] == "spawn_unregistered"
        assert payload["ticket_id"] == "520"
        assert "surface_ref" in payload

    def test_event_payload_includes_surface_ref_and_timeout(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Unregistered event: surface_ref and poll_timeout_secs in payload."""
        from cw.events import read_events
        from cw.exceptions import SpawnUnregisteredError
        from cw.models import OrchestratorEventType
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True
        worktree = make_git_repo("wt-520-payload")

        with pytest.raises(SpawnUnregisteredError):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev 520 --headless",
                label="auto-dev-520",
                native_daemon=daemon,
                ticket_id="520",
                _roster_poll_timeout=0.0,
                _roster_poll_interval=0.0,
            )

        events = read_events(
            consumer="_test_520_payload",
            event_types=[OrchestratorEventType.SESSION_SPAWN_UNREGISTERED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["surface_ref"] == "00000001"
        assert payload["poll_timeout_secs"] == 0.0

    def test_fake_daemon_raise_unregistered_flag(self) -> None:
        """raise_unregistered=True: spawn_bg returns id absent from live set."""
        from pathlib import Path as _Path

        daemon = FakeNativeDaemonClient()
        daemon.raise_unregistered = True

        short_id = daemon.spawn_bg(cwd=_Path("/tmp"), prompt="test")
        assert short_id == "00000001"
        assert short_id not in daemon.list_live_session_short_ids()

    def test_fake_daemon_default_registers_normally(self) -> None:
        """FakeNativeDaemonClient default: spawn_bg adds id to live set."""
        from pathlib import Path as _Path

        daemon = FakeNativeDaemonClient()

        short_id = daemon.spawn_bg(cwd=_Path("/tmp"), prompt="test")
        assert short_id in daemon.list_live_session_short_ids()

    def test_spawn_unregistered_error_is_subclass_of_cw_error(self) -> None:
        """SpawnUnregisteredError is a subclass of CwError (caught by dispatch loop)."""
        from cw.exceptions import CwError, SpawnUnregisteredError

        assert issubclass(SpawnUnregisteredError, CwError)


class TestSpawnCreateImplPostLaunchFailure:
    """#2502: a step that fails after ``daemon.spawn_bg`` returned.

    The worker is live by then, so ``spawn_create_impl`` must say so with
    :class:`WorkerLaunchedError` (never a plain error a caller would retry),
    page the operator once, and leave the worker alone: the leaked-worker sweep
    owns stopping a worker that no session names.
    """

    @staticmethod
    def _spawn(
        tmp_path: Path,
        worktree: Path,
        daemon: FakeNativeDaemonClient,
        *,
        roster_poll_timeout: float = 1.0,
    ) -> str:
        return spawn_create_impl(
            client=_make_client(tmp_path),
            worktree=worktree,
            prompt="/auto-dev 2502 --headless",
            label="auto-dev-2502",
            native_daemon=daemon,
            ticket_id="2502",
            lane="fast",
            _roster_poll_timeout=roster_poll_timeout,
        )

    def test_state_write_failure_raises_worker_launched_and_pages(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        capture_events: Callable[..., list[CapturedEvent]],
        fail_state_write_after_launch: None,
    ) -> None:
        """(b) save_state raising after launch: typed error, one page, no stop."""
        events = capture_events(
            "cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )

        with pytest.raises(WorkerLaunchedError) as excinfo:
            self._spawn(tmp_path, make_git_repo("wt-2502-state"), mock_native_daemon)

        err = excinfo.value
        assert err.surface_ref == "00000001"
        assert isinstance(err.__cause__, OSError)
        assert "simulated sessions.json write failure" in str(err)
        assert err.session_id in str(err)
        assert mock_native_daemon.stop_calls == []
        assert load_state().sessions == []
        payload = post_launch_attention_payload(events)
        assert payload["session_id"] == err.session_id
        assert payload["session_name"] == "test-client/auto-dev-2502"
        assert payload["client"] == "test-client"
        assert payload["ticket_id"] == "2502"
        assert payload["lane"] == "fast"
        assert payload["claude_session_id"] is None
        assert err.session_id in payload["breadcrumbs"]
        assert "00000001" in payload["breadcrumbs"]

    def test_csid_failure_raises_worker_launched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transcript-lookup failure after launch is post-launch too."""
        events = capture_events(
            "cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )

        def _boom(_sess: object) -> None:
            msg = "transcript unreadable"
            raise CwError(msg)

        monkeypatch.setattr("cw.spawn._csid_from_transcript", _boom)

        with pytest.raises(WorkerLaunchedError, match="transcript unreadable") as exc:
            self._spawn(tmp_path, make_git_repo("wt-2502-csid"), mock_native_daemon)

        assert exc.value.surface_ref == "00000001"
        assert mock_native_daemon.stop_calls == []
        assert (
            post_launch_attention_payload(events)["session_id"] == exc.value.session_id
        )

    def test_parent_vanished_after_launch_raises_worker_launched(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        capture_events: Callable[..., list[CapturedEvent]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The parent passing the pre-launch check but gone by the state write
        is post-launch: the worker is already running."""
        events = capture_events(
            "cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="parent01")
        real_spawn_bg = mock_native_daemon.spawn_bg

        def _drop_parent_on_launch(**kwargs: Any) -> str:
            save_state(CwState(sessions=[]))
            return real_spawn_bg(**kwargs)

        monkeypatch.setattr(mock_native_daemon, "spawn_bg", _drop_parent_on_launch)

        with pytest.raises(WorkerLaunchedError, match="Parent session not found"):
            spawn_create_impl(
                client=_make_client(tmp_path, name="child-client"),
                worktree=make_git_repo("wt-2502-parent"),
                prompt="/auto-dev 2502 --headless",
                label="auto-dev-2502",
                native_daemon=mock_native_daemon,
                parent="parent01",
            )

        assert mock_native_daemon.stop_calls == []
        assert load_state().sessions == []
        assert post_launch_attention_payload(events)["ticket_id"] is None

    def test_unregistered_worker_still_raises_spawn_unregistered(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """SpawnUnregisteredError keeps its backend-failure meaning (R4/R9)."""
        events = capture_events(
            "cw.spawn", OrchestratorEventType.SESSION_NEEDS_ATTENTION
        )
        mock_native_daemon.raise_unregistered = True

        with pytest.raises(SpawnUnregisteredError) as excinfo:
            self._spawn(
                tmp_path,
                make_git_repo("wt-2502-unreg"),
                mock_native_daemon,
                roster_poll_timeout=0.0,
            )

        assert not isinstance(excinfo.value, WorkerLaunchedError)
        assert events == []
        assert mock_native_daemon.stop_calls == []

    def test_page_failure_keeps_worker_launched_error(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        fail_state_write_after_launch: None,
    ) -> None:
        """A failed page write is logged and never replaces the original error."""

        def _no_inbox(*_args: object, **_kwargs: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr("cw.spawn.record_event", _no_inbox)

        with (
            caplog.at_level(logging.ERROR, logger="cw.spawn"),
            pytest.raises(WorkerLaunchedError),
        ):
            self._spawn(tmp_path, make_git_repo("wt-2502-page"), mock_native_daemon)

        assert any(
            "spawn_post_launch_failed" in r.getMessage()
            and r.levelno == logging.ERROR
            and r.exc_info is not None
            for r in caplog.records
        )

    def test_page_emitted_with_no_lock_held(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        mock_native_daemon: FakeNativeDaemonClient,
        monkeypatch: pytest.MonkeyPatch,
        fail_state_write_after_launch: None,
    ) -> None:
        """The page is written after sessions_lock released (ADR-0019)."""
        held_at_emit: list[tuple[object, ...]] = []

        def _record(*_args: object, **_kwargs: object) -> None:
            held_at_emit.append(held_locks())

        monkeypatch.setattr("cw.spawn.record_event", _record)

        with pytest.raises(WorkerLaunchedError):
            self._spawn(tmp_path, make_git_repo("wt-2502-lock"), mock_native_daemon)

        assert held_at_emit == [()]

    @pytest.mark.parametrize(
        ("surface_ref", "prefix"),
        [
            ("00000001", "worker live (session sess-1, surface 00000001): "),
            (None, "worker live (session sess-1): "),
        ],
    )
    def test_breadcrumbs_redacted_single_line_and_truncated(
        self,
        capture_events: Callable[..., list[CapturedEvent]],
        surface_ref: str | None,
        prefix: str,
    ) -> None:
        """The breadcrumb names the live session; the error is made safe."""
        events = capture_events("cw.spawn")
        error = "first line\nsecond Bearer abc123 " + "y" * 600

        emit_spawn_post_launch_attention(
            session_id="sess-1",
            session_name="",
            client="test-client",
            ticket_id="2502",
            lane=None,
            claude_session_id=None,
            surface_ref=surface_ref,
            error=error,
        )

        payload = post_launch_attention_payload(events)
        crumbs = payload["breadcrumbs"]
        assert crumbs.startswith(prefix)
        detail = crumbs.removeprefix(prefix)
        assert "\n" not in crumbs
        assert "abc123" not in crumbs
        assert detail.startswith("first line second <redacted>")
        assert len(detail) == _BREADCRUMB_DETAIL_MAX + 1
        assert detail.endswith("…")
        assert events[0][2] == "2502"


# ---------------------------------------------------------------------------


class TestPriorAttemptsSummary:
    """Tests for #838: prior_attempts_summary populated on retry."""

    def test_attempts_zero_produces_empty_list(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """attempts=0 → prior_attempts_summary is always []."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-zero-attempts")
        task = _make_pending_task(ticket_id="838-A", attempts=0)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-A --headless",
            label="auto-dev/838-A",
            native_daemon=daemon,
            ticket_id="838-A",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["world_state_snapshot"]["prior_attempts_summary"] == []

    def test_no_matching_sessions_in_state(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """attempts=1 but no prior sessions for this ticket → empty list."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-no-match")
        _seed_completed_session(tmp_path, tmp_config_dir, ticket_id="OTHER-99")
        task = _make_pending_task(ticket_id="838-B", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-B --headless",
            label="auto-dev/838-B",
            native_daemon=daemon,
            ticket_id="838-B",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["world_state_snapshot"]["prior_attempts_summary"] == []

    def test_collect_prior_attempts_summary_unchanged_read_path_for_mixed_ages(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """#1983 lock-in: the read path is comment-only-changed, still hot-file only.

        No prune_sessions() involved — an old and a recent terminal session for
        the same (client, ticket_id) are both returned, ascending by
        completed_at, exactly as before the retention work.
        """
        from cw.spawn import _collect_prior_attempts_summary

        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="1983-A",
            completed_at=datetime(2026, 6, 1, tzinfo=UTC),
            last_result={"status": "blocked"},
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="1983-A",
            completed_at=datetime(2025, 1, 1, tzinfo=UTC),
            last_result={"status": "no_op"},
        )

        summaries = _collect_prior_attempts_summary("1983-A", client="test-client")
        assert [s["status"] for s in summaries] == ["no_op", "blocked"]

    def test_timed_out_session_with_sentinel_produces_summary(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """TIMED_OUT session with last_result → one compact summary entry."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-timed-out")
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage2_impl",
            "blocker": {"stage": "s2", "reason": "impl_failed", "details": "tests red"},
            "friction_highlights": ["mypy error in foo.py"],
        }
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-C",
            status=SessionStatus.TIMED_OUT,
            last_result=last_result,
        )
        task = _make_pending_task(ticket_id="838-C", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-C --headless",
            label="auto-dev/838-C",
            native_daemon=daemon,
            ticket_id="838-C",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        s = summaries[0]
        assert s["status"] == "blocked"
        assert s["stage_reached"] == "stage2_impl"
        assert s["blocker_reason"] == "impl_failed"
        assert s["blocker_details"] == "tests red"
        assert s["friction_highlights"] == ["mypy error in foo.py"]

    def test_completed_session_with_sentinel_produces_summary(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """COMPLETED session with last_result → summary entry included."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-completed")
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage3_review",
            "blocker": {"stage": "s3", "reason": "review_blocked", "details": ""},
            "friction_highlights": [],
        }
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-D",
            status=SessionStatus.COMPLETED,
            last_result=last_result,
        )
        task = _make_pending_task(ticket_id="838-D", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-D --headless",
            label="auto-dev/838-D",
            native_daemon=daemon,
            ticket_id="838-D",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        assert summaries[0]["status"] == "blocked"
        assert summaries[0]["stage_reached"] == "stage3_review"

    def test_no_sentinel_produces_no_sentinel_entry(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """TIMED_OUT with last_result=None → entry with status='no_sentinel'."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-no-sentinel")
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-E",
            status=SessionStatus.TIMED_OUT,
            last_result=None,
        )
        task = _make_pending_task(ticket_id="838-E", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-E --headless",
            label="auto-dev/838-E",
            native_daemon=daemon,
            ticket_id="838-E",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        s = summaries[0]
        assert s["status"] == "no_sentinel"
        assert s["stage_reached"] is None

    def test_multiple_prior_sessions_sorted_by_completed_at(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Multiple prior sessions → sorted chronologically by completed_at."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-sorted")

        earlier = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        later = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-F",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {"stage": "s2", "reason": "first", "details": ""},
                "friction_highlights": [],
            },
            completed_at=later,
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-F",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage1_plan",
                "blocker": {"stage": "s1", "reason": "second", "details": ""},
                "friction_highlights": [],
            },
            completed_at=earlier,
        )
        task = _make_pending_task(ticket_id="838-F", attempts=2)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-F --headless",
            label="auto-dev/838-F",
            native_daemon=daemon,
            ticket_id="838-F",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 2
        assert summaries[0]["blocker_reason"] == "second"
        assert summaries[1]["blocker_reason"] == "first"

    def test_state_read_failure_returns_empty_list(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """load_state() failure → _collect_prior_attempts_summary falls back to []."""
        import unittest.mock

        from cw.spawn import _collect_prior_attempts_summary

        with unittest.mock.patch(
            "cw.spawn.load_state", side_effect=OSError("disk full")
        ):
            result = _collect_prior_attempts_summary("838-H", client="test-client")

        assert result == []

    def test_blocker_details_truncated_to_500_chars(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """blocker.details > 500 chars is truncated to 500 in the summary."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-838-truncate")
        long_details = "x" * 600
        last_result: dict[str, object] = {
            "status": "blocked",
            "stage_reached": "stage2_impl",
            "blocker": {
                "stage": "s2",
                "reason": "impl_failed",
                "details": long_details,
            },
            "friction_highlights": [],
        }
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="838-G",
            status=SessionStatus.TIMED_OUT,
            last_result=last_result,
        )
        task = _make_pending_task(ticket_id="838-G", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 838-G --headless",
            label="auto-dev/838-G",
            native_daemon=daemon,
            ticket_id="838-G",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        assert len(summaries[0]["blocker_details"]) == 500

    def test_cross_client_same_ticket_number_not_leaked(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#1839: two clients dispatching the same ticket number must not

        cross-contaminate prior_attempts_summary. Seeds a TIMED_OUT session
        for "definitely-not-digimon"/47 (foreign-codebase marker
        "ArcSkeleton") and one for "review-bingo"/47 (distinct marker), then
        dispatches review-bingo/47 and asserts only review-bingo's own prior
        attempt appears -- not just a count of 1, but the *right* entry.
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="review-bingo")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-1839-review-bingo")

        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="definitely-not-digimon",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {
                    "stage": "s2",
                    "reason": "impl_failed",
                    "details": "ArcSkeleton",
                },
                "friction_highlights": [],
            },
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="review-bingo",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {
                    "stage": "s2",
                    "reason": "impl_failed",
                    "details": "bingo card render mismatch",
                },
                "friction_highlights": [],
            },
        )
        task = _make_pending_task(ticket_id="47", client="review-bingo", attempts=1)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 47 --headless",
            label="auto-dev/47",
            native_daemon=daemon,
            ticket_id="47",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        assert summaries[0]["blocker_details"] == "bingo card render mismatch"
        assert all(s["blocker_details"] != "ArcSkeleton" for s in summaries)

    def test_cross_client_same_ticket_number_not_leaked_symmetric(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#1839 symmetric case: dispatching the *other* client on the same

        ticket number must see only its own prior attempt. Guards against an
        off-by-one/inverted-condition fix that happens to pass the
        review-bingo-direction test by accident.
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="definitely-not-digimon")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-1839-digimon")

        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="definitely-not-digimon",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {
                    "stage": "s2",
                    "reason": "impl_failed",
                    "details": "ArcSkeleton",
                },
                "friction_highlights": [],
            },
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="review-bingo",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {
                    "stage": "s2",
                    "reason": "impl_failed",
                    "details": "bingo card render mismatch",
                },
                "friction_highlights": [],
            },
        )
        task = _make_pending_task(
            ticket_id="47", client="definitely-not-digimon", attempts=1
        )

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 47 --headless",
            label="auto-dev/47",
            native_daemon=daemon,
            ticket_id="47",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 1
        assert summaries[0]["blocker_details"] == "ArcSkeleton"
        assert all(
            s["blocker_details"] != "bingo card render mismatch" for s in summaries
        )

    def test_cross_client_filter_composes_with_chronological_sort(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """#1839: client filter composes correctly with the existing

        completed_at sort, not just with a single-entry case. Seeds 2
        review-bingo sessions (T0, T2) and 1 other-client session for the
        same ticket number interleaved between them (T1); asserts the
        returned list has length 2 (not 3), both entries belong to
        review-bingo, and they remain sorted ascending.
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path, name="review-bingo")
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-1839-sorted-filtered")

        t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="review-bingo",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage1_plan",
                "blocker": {"stage": "s1", "reason": "rb-first", "details": ""},
                "friction_highlights": [],
            },
            completed_at=t0,
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="other-client",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage2_impl",
                "blocker": {"stage": "s2", "reason": "other-attempt", "details": ""},
                "friction_highlights": [],
            },
            completed_at=t1,
        )
        _seed_completed_session(
            tmp_path,
            tmp_config_dir,
            ticket_id="47",
            client="review-bingo",
            status=SessionStatus.TIMED_OUT,
            last_result={
                "status": "blocked",
                "stage_reached": "stage3_review",
                "blocker": {"stage": "s3", "reason": "rb-second", "details": ""},
                "friction_highlights": [],
            },
            completed_at=t2,
        )
        task = _make_pending_task(ticket_id="47", client="review-bingo", attempts=2)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 47 --headless",
            label="auto-dev/47",
            native_daemon=daemon,
            ticket_id="47",
            headless=True,
            task=task,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        summaries = context["world_state_snapshot"]["prior_attempts_summary"]
        assert len(summaries) == 2
        assert summaries[0]["blocker_reason"] == "rb-first"
        assert summaries[1]["blocker_reason"] == "rb-second"


# ---------------------------------------------------------------------------
# Static guard: no daemon stop under sessions_lock (#2547)
# ---------------------------------------------------------------------------


class _StopUnderSessionsLockFinder(ast.NodeVisitor):
    """Collect ``.stop(`` call lines lexically nested in ``with sessions_lock(...)``.

    ADR-0019's runtime harness only sees real subprocess execs, so a fake
    daemon stop under the lock is invisible to it; this scan covers that gap.
    """

    def __init__(self) -> None:
        self._lock_depth = 0
        self.locked_blocks = 0
        self.stop_lines: list[int] = []

    @staticmethod
    def _takes_sessions_lock(node: ast.With) -> bool:
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "sessions_lock":
                return True
        return False

    def visit_With(self, node: ast.With) -> None:
        locked = self._takes_sessions_lock(node)
        if locked:
            self.locked_blocks += 1
            self._lock_depth += 1
        self.generic_visit(node)
        if locked:
            self._lock_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        if (
            self._lock_depth > 0
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stop"
        ):
            self.stop_lines.append(node.lineno)
        self.generic_visit(node)


def test_spawn_cli_never_stops_a_daemon_under_sessions_lock() -> None:
    """``cw spawn close`` / ``complete`` stop the surface after the lock (#2547)."""
    path = _SRC_ROOT / "cw" / "cli" / "spawn.py"
    finder = _StopUnderSessionsLockFinder()

    finder.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))

    # Non-vacuous: close (1) + complete (1) take the lock; a rename would
    # otherwise make this scan silently pass.
    assert finder.locked_blocks >= 2
    assert finder.stop_lines == [], (
        f"daemon .stop( under sessions_lock in cw/cli/spawn.py at lines "
        f"{finder.stop_lines}: capture the surface_ref under the lock and stop "
        "after it releases (ADR-0019, #2547)"
    )
