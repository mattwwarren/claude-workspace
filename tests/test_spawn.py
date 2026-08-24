"""Tests for cw spawn and spawn close commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

import pytest
from click.testing import CliRunner

from cw.auto_dev_result import AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION, Status
from cw.cli import main
from cw.config import load_state, orchestrator_config_file, save_state
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
from cw.spawn import build_disallowed_tools_arg
from tests.conftest import _make_ticket_task, _seed_daemon_session

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


class TestHookSettingsTemplate:
    """The settings.local.json template wires both hooks (#940 R5 + #147)."""

    def test_template_has_pretooluse_guard_and_preserves_stop(self) -> None:
        """PreToolUse/Bash/cw guard-cwd is present; Stop/cw signal-stop preserved."""
        from cw.spawn import _HOOK_SETTINGS_TEMPLATE

        hooks = _HOOK_SETTINGS_TEMPLATE["hooks"]

        stop_entries = hooks["Stop"]
        assert any(
            entry["hooks"][0]["command"] == "cw signal-stop" for entry in stop_entries
        )

        pretooluse_entries = hooks["PreToolUse"]
        assert any(
            entry.get("matcher") == "Bash"
            and entry["hooks"][0]["command"] == "cw guard-cwd"
            for entry in pretooluse_entries
        )

    def test_template_includes_busy_wait_guard_pretooluse(self) -> None:
        """#1946: guard-busy-wait rides the SAME Bash entry as guard-cwd.

        Encodes the shape directly: exactly one "Bash"-matched PreToolUse
        entry exists, and both commands sit in that one entry's hooks list.
        A second top-level "Bash" entry would be a different (unverified)
        dispatch question about how Claude Code handles duplicate matchers.
        """
        from cw.spawn import _HOOK_SETTINGS_TEMPLATE

        entries = _HOOK_SETTINGS_TEMPLATE["hooks"]["PreToolUse"]
        bash_entries = [e for e in entries if e.get("matcher") == "Bash"]
        assert len(bash_entries) == 1

        commands = [hook["command"] for hook in bash_entries[0]["hooks"]]
        assert commands == ["cw guard-cwd", "cw guard-busy-wait"]

    def test_hook_settings_template_includes_agent_spawn_pretooluse(self) -> None:
        """#1646: a subagent-tool PreToolUse entry sits alongside the Bash guard."""
        from cw.spawn import _AGENT_TOOL_MATCHER, _HOOK_SETTINGS_TEMPLATE

        entries = _HOOK_SETTINGS_TEMPLATE["hooks"]["PreToolUse"]
        assert any(
            entry.get("matcher") == _AGENT_TOOL_MATCHER
            and entry["hooks"][0]["command"] == "cw agent-spawn-pre"
            for entry in entries
        )
        # Must not regress the pre-existing Bash guard entry.
        assert any(entry.get("matcher") == "Bash" for entry in entries)

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
        from cw.spawn import _HOOK_SETTINGS_TEMPLATE

        assert "PostToolUse" not in _HOOK_SETTINGS_TEMPLATE["hooks"]

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
        """cw-context.json schema_version is current (6 after the #1946 addition)."""
        from cw.spawn import CW_CONTEXT_SCHEMA_VERSION, spawn_create_impl

        assert CW_CONTEXT_SCHEMA_VERSION == 6

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
        assert context["schema_version"] == 6


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
        from cw.spawn import _HOOK_SETTINGS_TEMPLATE

        rendered = json.dumps(_HOOK_SETTINGS_TEMPLATE)
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


# ---------------------------------------------------------------------------
# Tests for #838: prior_attempts_summary populated on retry
# ---------------------------------------------------------------------------


def _seed_completed_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    ticket_id: str,
    client: str = "test-client",
    status: SessionStatus = SessionStatus.TIMED_OUT,
    last_result: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> Session:
    """Seed a TIMED_OUT or COMPLETED session for a given ticket in state."""
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    sess = Session(
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=status,
        workspace_path=workspace,
        last_result=last_result,
        completed_at=completed_at or datetime.now(UTC),
    )
    state = load_state()
    state.sessions.append(sess)
    save_state(state)
    return sess


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
