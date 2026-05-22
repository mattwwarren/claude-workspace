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

if TYPE_CHECKING:
    pass


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
# Unit-level tests (no Click runner, adapter injected directly)
# ---------------------------------------------------------------------------


class TestSpawnCreate:
    """Tests for the spawn_create business logic."""

    def test_happy_path_creates_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create: happy path stores session with correct fields."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path, "Implement the feature.")
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree" / "feat-branch"
        worktree.mkdir(parents=True)

        session_id = _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            surface="split",
            label="my-task",
            adapter=adapter,
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
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create: default label produces 'client/daemon' session name."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            surface="split",
            label=None,
            adapter=adapter,
        )

        state = load_state()
        assert state.sessions[0].name == "test-client/daemon"

    def test_adapter_receives_correct_spawn_args(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create: adapter.spawn is called with workspace, command, surface."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path, name="acme")
        prompt_content = "Fix the login bug."
        prompt_file = _make_prompt_file(tmp_path, prompt_content)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            surface="tab",
            label=None,
            adapter=adapter,
        )

        assert len(adapter.calls["spawn"]) == 1
        workspace_arg, command_arg, surface_arg = adapter.calls["spawn"][0]
        assert workspace_arg == "acme"
        assert str(worktree) in command_arg
        assert prompt_content in command_arg
        assert surface_arg == "tab"
        # Regression guard: claude's -w takes a worktree name, not a path. The
        # spawn command must cd into the worktree instead of passing it via -w.
        assert " -w " not in command_arg
        assert command_arg.startswith("cd ")
        # Daemon spawns go through `cw run-claude` so the wrapper can emit a
        # SESSION_COMPLETED on clean exit (issue #99). Env vars carry the
        # session identity through to the wrapper.
        assert "cw run-claude -- --print" in command_arg
        assert "CW_CLIENT=acme" in command_arg
        assert "CW_PURPOSE=impl" in command_arg
        assert "CW_SESSION_ID=" in command_arg

    def test_cmux_workspace_overrides_client_name(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create: cmux_workspace field is used as workspace arg if set."""
        from cw.cli import _spawn_create_impl

        workspace = tmp_path / "workspace" / "acme"
        workspace.mkdir(parents=True)
        client = ClientConfig(
            name="acme",
            workspace_path=workspace,
            cmux_workspace="my-custom-ws",
        )
        prompt_file = _make_prompt_file(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            surface="split",
            label=None,
            adapter=adapter,
        )

        workspace_arg, _cmd, _surface = adapter.calls["spawn"][0]
        assert workspace_arg == "my-custom-ws"

    def test_surface_ref_stored_from_adapter(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create: surface_ref on session matches the adapter return value."""
        from cw.cli import _spawn_create_impl

        client = _make_client(tmp_path)
        prompt_file = _make_prompt_file(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        _spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt_file=prompt_file,
            surface="split",
            label=None,
            adapter=adapter,
        )

        state = load_state()
        # FakeCmuxAdapter returns "fake-pane-1" for first spawn call
        assert state.sessions[0].surface_ref == "fake-pane-1"

    def test_parent_linkage_writes_both_directions(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create with parent: worker.parent_session_id set and parent's
        worker_session_ids list contains the new worker id (single state save).
        """
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

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
            surface="split",
            label="auto-dev-GEN-9",
            adapter=adapter,
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
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn create with bogus parent ID: CwError, no session created, no spawn."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        with pytest.raises(CwError, match="Parent session not found"):
            spawn_create_impl(
                client=client,
                worktree=worktree,
                prompt="/auto-dev GEN-9 --headless",
                surface="split",
                label=None,
                adapter=adapter,
                parent="does-not-exist",
            )

        # No worker session persisted, no surface spawned.
        state = load_state()
        assert state.sessions == []
        assert adapter.calls["spawn"] == []


class TestHookContextInjection:
    """Tests for the Stop-hook + cw-context file injection (issue #147)."""

    def test_writes_settings_and_context_files(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn_create_impl writes .claude/settings.local.json + cw-context.json."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        session_id = spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="/auto-dev 137 --headless",
            surface="split",
            label="auto-dev/137",
            adapter=adapter,
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
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """When no ticket_id is supplied, cw-context.json carries null."""
        from cw.spawn import spawn_create_impl

        client = _make_client(tmp_path)
        adapter = FakeCmuxAdapter()
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)

        spawn_create_impl(
            client=client,
            worktree=worktree,
            prompt="just do it",
            surface="split",
            label=None,
            adapter=adapter,
        )

        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context["ticket_id"] is None


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
            surface_ref="fake-pane-99",
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

        _spawn_close_impl(session_id=sess.id, adapter=adapter)

        state = load_state()
        closed = state.find_by_name_or_id(sess.id)
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED
        assert closed.completed_reason == CompletionReason.USER
        assert closed.completed_at is not None

    def test_adapter_close_called_with_surface_ref(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: adapter.close receives the session's surface_ref."""
        from cw.cli import _spawn_close_impl

        sess = self._seed_daemon_session(tmp_path, tmp_config_dir)
        adapter = FakeCmuxAdapter()

        _spawn_close_impl(session_id=sess.id, adapter=adapter)

        assert len(adapter.calls["close"]) == 1
        assert adapter.calls["close"][0] == ("fake-pane-99",)

    def test_missing_session_raises_cw_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: raises CwError when session_id not found."""
        from cw.cli import _spawn_close_impl

        adapter = FakeCmuxAdapter()
        error_msg = ""
        try:
            _spawn_close_impl(session_id="nonexistent", adapter=adapter)
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

        with pytest.raises(CwError, match="already completed"):
            _spawn_close_impl(session_id="done1234", adapter=adapter)

    def test_no_surface_ref_skips_adapter_close(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """spawn close: adapter.close NOT called if surface_ref is None."""
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

        _spawn_close_impl(session_id="nosurf1", adapter=adapter)

        assert adapter.calls["close"] == []
        state = load_state()
        closed = state.find_by_name_or_id("nosurf1")
        assert closed is not None
        assert closed.status == SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# CLI integration tests via Click CliRunner
# ---------------------------------------------------------------------------


class TestSpawnCLI:
    """CLI-layer tests using CliRunner (adapter injected via env/monkeypatch)."""

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
