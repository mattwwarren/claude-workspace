"""Tests for cw spawn and spawn close commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cmux import FakeCmuxAdapter
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
)
from cw.native_daemon import FakeNativeDaemonClient

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
        ``cw run-claude`` indirection.
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


class TestSpawnClose:
    """Tests for the spawn close business logic."""

    def _seed_daemon_session(self, tmp_path: Path, tmp_config_dir: Path) -> Session:
        """Save a DAEMON session to state and return it."""
        workspace = tmp_path / "workspace" / "test-client"
        workspace.mkdir(parents=True)
        sess = Session(
            id="dead1234",
            name="test-client/my-task",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=workspace,
            surface_ref="abc12345",
        )
        state = CwState(sessions=[sess])
        save_state(state)
        return sess

    def test_happy_path_marks_completed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: session marked COMPLETED after close."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id=sess.id, adapter=adapter, native_daemon=daemon)

        state = load_state()
        closed = state.find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.completed_at is not None

    def test_daemon_close_routes_through_native_daemon(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON-origin sessions get stopped via claude stop, not adapter.close."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id=sess.id, adapter=adapter, native_daemon=daemon)

        assert daemon.stop_calls == ["abc12345"]
        assert adapter.calls["close"] == []

    def test_user_origin_close_routes_through_adapter(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """USER-origin sessions still close via the multiplexer adapter."""
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
        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id="user0001", adapter=adapter, native_daemon=daemon)

        assert adapter.calls["close"] == [("tmux-pane-7",)]
        assert daemon.stop_calls == []

    def test_missing_session_raises_cw_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: raises CwError when session_id not found."""
        from cw.cli import _spawn_close_impl

        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()
        error_msg = ""
        try:
            _spawn_close_impl(
                session_id="nonexistent", adapter=adapter, native_daemon=daemon
            )
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
        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()

        with pytest.raises(CwError, match="already completed"):
            _spawn_close_impl(
                session_id="done1234", adapter=adapter, native_daemon=daemon
            )

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
        adapter = FakeCmuxAdapter()
        daemon = FakeNativeDaemonClient()

        _spawn_close_impl(session_id="nosurf1", adapter=adapter, native_daemon=daemon)

        assert adapter.calls["close"] == []
        assert daemon.stop_calls == []
        state = load_state()
        closed = state.find_by_name_or_id("nosurf1")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED


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
