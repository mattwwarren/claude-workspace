"""Tests for cw spawn and spawn close commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from click.testing import CliRunner

from cw.auto_dev_result import Status
from cw.cli import main
from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.spawn import _LINEAR_MCP_DISALLOW

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _seed_daemon_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    session_id: str = "test1234",
    client: str = "test-client",
    name: str | None = None,
    surface_ref: str | None = "fake-pane-99",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """Create and save a daemon session in state."""
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    sess = Session(
        id=session_id,
        name=name or f"{client}/auto-dev/GEN-42",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=status,
        workspace_path=workspace,
        surface_ref=surface_ref,
    )
    state = CwState(sessions=[sess])
    save_state(state)
    return sess


def _seed_running_task(
    ticket_id: str = "GEN-42",
    client: str = "test-client",
    session_id: str = "test1234",
) -> TicketTask:
    """Create and save a RUNNING TicketTask in the dev queue."""
    from cw.dev_queue import save_dev_queue
    from cw.models import DevQueueStore, QueueItemStatus

    task = TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
    )
    store = DevQueueStore(tasks=[task])
    save_dev_queue(store)
    return task


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
            entry["hooks"][0]["command"] == "cw signal-stop" for entry in stop_hooks
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
            entry["hooks"][0]["command"] == "cw signal-stop" for entry in stop_hooks
        )
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
        stop_hooks = settings["hooks"]["Stop"]
        assert any(
            entry["hooks"][0]["command"] == "cw signal-stop" for entry in stop_hooks
        )
        # Correlation file should still be written.
        context_path = worktree / ".claude" / "cw-context.json"
        assert context_path.exists()


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

    def test_settings_written_via_atomic_write(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings.local.json is written through atomic_write_text, not write_text.

        Verifies that atomic_write_text is called for the settings file so a
        concurrent reader never observes an empty/partial file (no O_TRUNC window).
        """
        import cw.spawn as spawn_mod

        calls: list[tuple[Path, str]] = []
        real_atomic = spawn_mod.atomic_write_text

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

        calls: list[tuple[Path, str]] = []
        real_atomic = spawn_mod.atomic_write_text

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

    def test_already_completed_raises_cw_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: raises CwError when session is already completed."""
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
        )
        save_state(CwState(sessions=[sess]))
        daemon = FakeNativeDaemonClient()

        with pytest.raises(CwError, match="already completed"):
            _spawn_close_impl(session_id="done1234", native_daemon=daemon)

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


# ---------------------------------------------------------------------------
# TestSpawnComplete
# ---------------------------------------------------------------------------


class TestSpawnComplete:
    """Tests for _spawn_complete_impl and cw spawn complete command."""

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
        """Session already COMPLETED + --force → no-op, no extra events."""
        from cw.cli import _spawn_complete_impl
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        sess = _seed_daemon_session(
            tmp_path, tmp_config_dir, status=SessionStatus.COMPLETED
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

    return TicketTask(
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
        assert ref["version"] == 4
        assert "cw schema show" in ref["command"]
        qm = context["queue_metadata"]
        assert qm["scope_hint"] == "large"
        assert qm["plan_source"] is None
        assert qm["headless_timeout_override"] is None
        ws = context["world_state_snapshot"]
        assert ws["origin_main_branch"] == "main"
        assert ws["prior_attempts_summary"] == []

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

        import cw.spawn as spawn_mod
        from cw.spawn import spawn_create_impl

        fake_sha = "abc1234def5678"
        real_run = subprocess_mod.run

        def patched_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess_mod.CompletedProcess[str]:
            if "rev-parse" in cmd and "origin/main" in cmd:
                return subprocess_mod.CompletedProcess(
                    cmd, 0, stdout=fake_sha + "\n", stderr=""
                )
            return real_run(cmd, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(spawn_mod.subprocess, "run", patched_run)

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
# Tests for #726: Linear MCP disallow-tools when tracker is github-issues
# ---------------------------------------------------------------------------


def _write_project_config(workspace: Path, system: str) -> None:
    """Write a minimal .claude/project-config.yaml with the given tracker system."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    config = f"tracking:\n  primary:\n    system: {system}\n"
    (claude_dir / "project-config.yaml").write_text(config, encoding="utf-8")


class TestLinearMcpDisallow:
    """Tests for #726: --disallowed-tools is injected when tracker=github-issues.

    When a client's project-config.yaml declares github-issues as the tracker,
    spawn_create_impl must add --disallowed-tools mcp__plugin_linear_linear__*
    to the claude --bg extra_args so the Linear MCP is unreachable in the
    headless worker (where OAuth cannot complete).
    """

    def test_github_issues_tracker_injects_disallow_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """github-issues tracker → --disallowed-tools mcp__plugin_linear_linear__*."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "gh-client"
        workspace.mkdir(parents=True)
        _write_project_config(workspace, "github-issues")
        client = ClientConfig(name="gh-client", workspace_path=workspace)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-github-issues")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] == [
            "--disallowed-tools",
            _LINEAR_MCP_DISALLOW,
        ]

    def test_linear_tracker_does_not_inject_disallow_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """linear tracker → no --disallowed-tools flag."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "lin-client"
        workspace.mkdir(parents=True)
        _write_project_config(workspace, "linear")
        client = ClientConfig(name="lin-client", workspace_path=workspace)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-linear")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev LIN-99 --headless",
            label="auto-dev-LIN-99",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] is None

    def test_absent_project_config_does_not_inject_disallow_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """No project-config.yaml → no --disallowed-tools flag (fail-open)."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)  # no project-config.yaml
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-no-config")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] is None

    def test_github_issues_with_worker_model_orders_model_before_disallow(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """github-issues + worker_model: --model first, then --disallowed-tools."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "gh-model-client"
        workspace.mkdir(parents=True)
        _write_project_config(workspace, "github-issues")
        client = ClientConfig(
            name="gh-model-client",
            workspace_path=workspace,
            worker_model="claude-sonnet-4-6-20251015",
        )
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-model-disallow")

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
            "--disallowed-tools",
            _LINEAR_MCP_DISALLOW,
        ]

    def test_github_issues_with_extra_args_appended_after_disallow(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Caller extra_args append after --disallowed-tools."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "gh-extra-client"
        workspace.mkdir(parents=True)
        _write_project_config(workspace, "github-issues")
        client = ClientConfig(name="gh-extra-client", workspace_path=workspace)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-extra-args")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
            extra_args=["--resume", "abc12345"],
        )

        assert daemon.spawn_extra_args[0] == [
            "--disallowed-tools",
            _LINEAR_MCP_DISALLOW,
            "--resume",
            "abc12345",
        ]

    def test_corrupt_project_config_does_not_inject_disallow_flag(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Corrupt project-config.yaml → no --disallowed-tools (fail-open)."""
        from cw.spawn import spawn_create_impl

        workspace = tmp_path / "workspace" / "corrupt-client"
        workspace.mkdir(parents=True)
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "project-config.yaml").write_text(
            ": invalid yaml {{{", encoding="utf-8"
        )
        client = ClientConfig(name="corrupt-client", workspace_path=workspace)
        daemon = FakeNativeDaemonClient()
        worktree = make_git_repo("wt-726-corrupt-config")

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 726 --headless",
            label="auto-dev-726",
            native_daemon=daemon,
        )

        assert daemon.spawn_extra_args[0] is None
