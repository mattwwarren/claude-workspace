"""Tests for cw.cli - Click CLI dispatcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import (
    _complete_client,
    _complete_session,
    _display_sessions,
    _display_status,
    main,
)
from cw.config import load_clients, load_state, save_state
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    QueueItem,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest


class TestCli:
    def test_version(self) -> None:
        from cw import __version__

        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Claude Workspace" in result.output

    def test_start_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(main, ["start", "my-client"])
            mock_start.assert_called_once_with(
                "my-client",
                "impl",
                worktree=None,
                parent=None,
            )

    def test_start_with_purpose(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(main, ["start", "--purpose", "idea", "my-client"])
            mock_start.assert_called_once_with(
                "my-client",
                "idea",
                worktree=None,
                parent=None,
            )

    def test_start_with_worktree(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(
                main,
                ["start", "--worktree", "feat/search", "my-client"],
            )
            mock_start.assert_called_once_with(
                "my-client",
                "impl",
                worktree="feat/search",
                parent=None,
            )

    def test_start_with_parent(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(
                main,
                ["start", "--parent", "abc12345", "my-client"],
            )
            mock_start.assert_called_once_with(
                "my-client",
                "impl",
                worktree=None,
                parent="abc12345",
            )

    def test_start_without_parent_passes_none(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(main, ["start", "my-client"])
            _, kwargs = mock_start.call_args
            assert kwargs.get("parent") is None

    def test_bg_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg"])
            mock_bg.assert_called_once_with(None, notify=None, auto=False)

    def test_bg_with_session_name(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg", "personal/debt"])
            mock_bg.assert_called_once_with(
                "personal/debt",
                notify=None,
                auto=False,
            )

    def test_resume_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.resume_session") as mock_resume:
            runner.invoke(main, ["resume", "my-session"])
            mock_resume.assert_called_once_with("my-session")

    def test_list_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli._display_sessions") as mock_list:
            runner.invoke(main, ["list"])
            mock_list.assert_called_once()

    def test_status_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli._display_status") as mock_status:
            runner.invoke(main, ["status"])
            mock_status.assert_called_once()

    def test_done_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.done_session") as mock_done:
            runner.invoke(main, ["done", "my-session"])
            mock_done.assert_called_once_with(
                "my-session",
                cleanup=False,
                force=False,
            )

    def test_done_with_cleanup(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.done_session") as mock_done:
            runner.invoke(main, ["done", "my-session", "--cleanup", "--force"])
            mock_done.assert_called_once_with(
                "my-session",
                cleanup=True,
                force=True,
            )

    def test_done_no_session_arg(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.done_session") as mock_done:
            runner.invoke(main, ["done"])
            mock_done.assert_called_once_with(
                None,
                cleanup=False,
                force=False,
            )

    def test_config_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.show_config") as mock_config:
            runner.invoke(main, ["config"])
            mock_config.assert_called_once()

    def test_error_display(self) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.start_session",
            side_effect=CwError("Test error message"),
        ):
            result = runner.invoke(main, ["start", "bad-client"])
            assert result.exit_code != 0
            assert "Test error message" in result.output


class TestUpgradeWorkers:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        completed = SimpleNamespace(
            returncode=0, stdout="Respawned 3 workers\n", stderr=""
        )
        with patch("cw.cli.subprocess.run", return_value=completed) as mock_run:
            result = runner.invoke(main, ["upgrade-workers"])
            mock_run.assert_called_once_with(
                ["claude", "respawn", "--all"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.exit_code == 0
            assert "Respawned 3 workers" in result.output

    def test_subprocess_nonzero_propagates_exit_code(self) -> None:
        runner = CliRunner()
        completed = SimpleNamespace(returncode=2, stdout="", stderr="boom\n")
        with patch("cw.cli.subprocess.run", return_value=completed):
            result = runner.invoke(main, ["upgrade-workers"])
            assert result.exit_code == 2
            assert "boom" in result.output

    def test_claude_not_on_path(self) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file or directory", "claude"),
        ):
            result = runner.invoke(main, ["upgrade-workers"])
            assert result.exit_code != 0
            assert "claude" in result.output
            assert "not found" in result.output

    def test_help_mentions_respawn(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["upgrade-workers", "--help"])
        assert result.exit_code == 0
        assert "respawn" in result.output


class TestSignalStop:
    """Tests for the `cw signal-stop` Stop-hook handler (issue #147)."""

    SEED_TICKET_ID = "137"
    """Default ticket_id stamped into the seeded cw-context.json. Tests that
    assert event payload propagation reference this constant rather than the
    bare string."""

    def _seed_session(self, tmp_path: Path, sess_id: str = "sess-147") -> Session:
        # Seed ACTIVE: a freshly-spawned DAEMON session is ACTIVE until the
        # Stop hook fires. After issue #165 Phase B the idempotency guard
        # covers IDLE too, so seeding IDLE would short-circuit every Stop
        # hook test that exercises the active → completed flow.
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        session = Session(
            id=sess_id,
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        return session

    def _write_context(
        self,
        worktree: Path,
        *,
        session_id: str,
        ticket_id: str | None = SEED_TICKET_ID,
    ) -> None:
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "session_name": "test-client/auto-dev/137",
                    "client": "test-client",
                    "purpose": "impl",
                    "ticket_id": ticket_id,
                }
            )
        )

    def test_happy_path_emits_event_and_completes_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = self._seed_session(tmp_path)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)
        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-abc",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
                "last_assistant_message": "all done",
            }
        )

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        state = load_state()
        updated = next(s for s in state.sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.claude_session_id == "claude-uuid-abc"

        events = read_events(
            consumer="test-signal-stop",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(
            e.payload.get("session_id") == session.id
            and e.payload.get("ticket_id") == self.SEED_TICKET_ID
            for e in events
        )

    def test_idempotent_when_already_completed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = self._seed_session(tmp_path)
        session.status = SessionStatus.COMPLETED
        state = load_state()
        state.sessions = [session]
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)
        hook_stdin = json.dumps({"session_id": "c-uuid", "cwd": str(worktree)})

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

        events = read_events(
            consumer="test-idem",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == session.id for e in events)

    def test_missing_context_file_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "no-context"
        worktree.mkdir()
        hook_stdin = json.dumps({"cwd": str(worktree), "session_id": "c-uuid"})

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

        events = read_events(
            consumer="test-missing-ctx",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert events == []

    def test_malformed_stdin_is_noop(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input="not json at all")
        assert result.exit_code == 0

    def test_empty_stdin_is_noop(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input="")
        assert result.exit_code == 0

    def test_cwd_not_string_is_noop(self, tmp_config_dir: Path) -> None:
        # Hook payload with non-string cwd → silent no-op.
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["signal-stop"],
            input=json.dumps({"cwd": 42, "session_id": "x"}),
        )
        assert result.exit_code == 0

    def test_corrupt_context_file_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "bad-ctx"
        (worktree / ".claude").mkdir(parents=True)
        (worktree / ".claude" / "cw-context.json").write_text("{not valid json")
        hook_stdin = json.dumps({"cwd": str(worktree), "session_id": "c-uuid"})

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

    def test_context_not_dict_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "list-ctx"
        (worktree / ".claude").mkdir(parents=True)
        # Valid JSON, but a list rather than a dict.
        (worktree / ".claude" / "cw-context.json").write_text("[1, 2, 3]")
        hook_stdin = json.dumps({"cwd": str(worktree), "session_id": "c-uuid"})

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

    def test_context_missing_session_id_is_noop(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "no-sid"
        (worktree / ".claude").mkdir(parents=True)
        (worktree / ".claude" / "cw-context.json").write_text(
            json.dumps({"client": "c", "ticket_id": "1"})  # no session_id
        )
        hook_stdin = json.dumps({"cwd": str(worktree), "session_id": "c-uuid"})

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

    def test_stdin_read_oserror_is_noop(self, tmp_config_dir: Path) -> None:
        """Direct-call the underlying callback with stdin raising OSError.

        CliRunner.invoke installs its own sys.stdin wrapper that out-races a
        patch on ``cw.cli.sys.stdin``, so this case is exercised by calling
        the command's callback directly with a fake stdin instead.
        """

        class _RaisingStdin:
            def read(self) -> str:
                error_msg = "broken pipe"
                raise OSError(error_msg)

        callback = main.commands["signal-stop"].callback
        assert callable(callback)
        with patch("cw.cli.sys.stdin", _RaisingStdin()):
            callback()

    def test_stops_native_bg_session_on_daemon_origin(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal-stop calls claude stop on a DAEMON session's short id.

        Without this cleanup the native bg session lingers in the daemon
        roster after its agent turn ends (Claude treats it as idle), and
        roster.json grows unbounded across dispatches — the exact failure
        mode GitHub issue #150 set out to retire. The Stop hook fires
        once per turn so the cleanup must live in signal-stop itself,
        not in a separate sweeper.
        """
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-stop")
        assert session.worktree_path is not None
        worktree = session.worktree_path
        # Register a short id with the fake daemon and stamp it on the
        # session — this mirrors what spawn_create_impl does in production.
        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        self._write_context(worktree, session_id=session.id)

        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-stop",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        assert daemon.stop_calls == [short_id]
        assert daemon.list_live_session_short_ids() == set()

    def test_does_not_stop_user_origin_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin sessions are not bg workers — leave them alone.

        signal-stop fires for any session whose worktree carries the
        injected hook context, but cleanup via ``claude stop`` only makes
        sense for daemon-origin bg workers. An interactive session that
        happened to land at the same cwd would not have a roster entry.
        """
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-user")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.origin = SessionOrigin.USER
        target.surface_ref = "tmux-pane-3"
        save_state(state)
        assert session.worktree_path is not None
        self._write_context(session.worktree_path, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {"session_id": "claude-uuid", "cwd": str(session.worktree_path)}
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0

        assert daemon.stop_calls == []

    def test_signal_stop_defers_when_background_tasks_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Stop hook with pending background_tasks must NOT complete.

        Dispatching a ``run_in_background: true`` subagent ends the parent's
        turn while the subagent is still running. The Stop hook fires with
        a populated ``background_tasks`` list; we must leave the session
        in its current status so the parent isn't killed mid-flight.
        See issue #151.
        """
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-bg")
        # The defers path needs a surface_ref so the regression assertion
        # below can prove daemon.stop wasn't called against any roster id.
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = "claude-short-id"
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        # Snapshot session state pre-call. The deferral path must leave it
        # byte-for-byte unchanged; asserting individual fields would mask
        # a regression that mutates some other field.
        pre_state = load_state()
        pre_target = next(s for s in pre_state.sessions if s.id == session.id)
        pre_snapshot = pre_target.model_dump()

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-bg",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
                "background_tasks": [
                    {"id": "task-1", "description": "Plan subagent running"},
                ],
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        # Deferral must not mutate session state. Whole-object snapshot
        # compare catches any field drift, not just status / claude_session_id.
        post_state = load_state()
        post_target = next(s for s in post_state.sessions if s.id == session.id)
        assert post_target.model_dump() == pre_snapshot

        # No SESSION_COMPLETED event must have been emitted.
        events = read_events(
            consumer="test-bg-defer",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == session.id for e in events)

        # native_daemon.stop must NOT have been called.
        assert daemon.stop_calls == []

    def test_signal_stop_proceeds_when_background_tasks_empty_list(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """An empty background_tasks list is treated as no pending work."""
        session = self._seed_session(tmp_path, sess_id="sess-bg-empty")
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)
        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-empty",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
                "background_tasks": [],
            }
        )

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        state = load_state()
        updated = next(s for s in state.sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.claude_session_id == "claude-uuid-empty"

        events = read_events(
            consumer="test-bg-empty",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(
            e.payload.get("session_id") == session.id
            and e.payload.get("ticket_id") == self.SEED_TICKET_ID
            for e in events
        )

    def test_signal_stop_proceeds_when_background_tasks_absent(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Payload without a background_tasks key behaves like no pending work.

        Explicit coverage so a future payload-normalization step can't
        silently regress the contract.
        """
        session = self._seed_session(tmp_path, sess_id="sess-bg-absent")
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)
        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-absent",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        state = load_state()
        updated = next(s for s in state.sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.claude_session_id == "claude-uuid-absent"

        events = read_events(
            consumer="test-bg-absent",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(
            e.payload.get("session_id") == session.id
            and e.payload.get("ticket_id") == self.SEED_TICKET_ID
            for e in events
        )

    def test_signal_stop_ignores_non_list_background_tasks(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A malformed background_tasks (non-list) is treated as no pending work.

        Mirrors the defensive-payload pattern used by test_cwd_not_string_is_noop
        and test_context_not_dict_is_noop.
        """
        session = self._seed_session(tmp_path, sess_id="sess-bg-bad")
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)
        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-bad",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
                "background_tasks": "not-a-list",
            }
        )

        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        state = load_state()
        updated = next(s for s in state.sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED

        events = read_events(
            consumer="test-bg-bad",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(e.payload.get("session_id") == session.id for e in events)

    # ------------------------------------------------------------------
    # Origin-aware Stop hook (issue #165, Phase B of multiplexer-removal).
    #
    # USER-origin sessions are interactive: the Stop hook fires at every
    # agent turn, but the human is still driving. Mark IDLE so daemon
    # triggers / wait loops can react, but DO NOT emit SESSION_COMPLETED
    # and DO NOT call native_daemon.stop (the session has no roster entry).
    # DAEMON-origin behavior must stay intact — that path completes the
    # session, emits the event, and stops the bg worker as before.
    # ------------------------------------------------------------------

    def test_signal_stop_user_origin_active_transitions_to_idle(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin + ACTIVE → IDLE, claude_session_id persisted, no event."""
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-user-active")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.origin = SessionOrigin.USER
        target.status = SessionStatus.ACTIVE
        target.surface_ref = "tmux-pane-9"
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-user",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.IDLE
        assert updated.claude_session_id == "claude-uuid-user"
        assert updated.completed_at is None
        assert updated.completed_reason is None

        events = read_events(
            consumer="test-user-active",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == session.id for e in events)

        assert daemon.stop_calls == []

    def test_signal_stop_user_origin_idle_is_noop(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin + IDLE: widened idempotency guard catches re-fires."""
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-user-idle")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.origin = SessionOrigin.USER
        target.status = SessionStatus.IDLE
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        pre_state = load_state()
        pre_target = next(s for s in pre_state.sessions if s.id == session.id)
        pre_snapshot = pre_target.model_dump()

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-idle-refire",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        post_target = next(s for s in load_state().sessions if s.id == session.id)
        assert post_target.model_dump() == pre_snapshot

        events = read_events(
            consumer="test-user-idle-refire",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == session.id for e in events)

        assert daemon.stop_calls == []

    def test_signal_stop_user_origin_backgrounded_is_noop(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """USER-origin + BACKGROUNDED: silent no-op via inside-branch ACTIVE check."""
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-user-bgrd")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.origin = SessionOrigin.USER
        target.status = SessionStatus.BACKGROUNDED
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path
        self._write_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        pre_state = load_state()
        pre_target = next(s for s in pre_state.sessions if s.id == session.id)
        pre_snapshot = pre_target.model_dump()

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-bgrd",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        post_target = next(s for s in load_state().sessions if s.id == session.id)
        assert post_target.model_dump() == pre_snapshot

        events = read_events(
            consumer="test-user-bgrd",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("session_id") == session.id for e in events)

        assert daemon.stop_calls == []

    def test_signal_stop_daemon_origin_unchanged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: DAEMON-origin still completes + emits + stops."""
        from cw.native_daemon import FakeNativeDaemonClient

        session = self._seed_session(tmp_path, sess_id="sess-daemon-regress")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.origin = SessionOrigin.DAEMON
        target.status = SessionStatus.ACTIVE
        save_state(state)
        assert session.worktree_path is not None
        worktree = session.worktree_path

        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        self._write_context(worktree, session_id=session.id)

        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-daemon",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.claude_session_id == "claude-uuid-daemon"

        events = read_events(
            consumer="test-daemon-regress",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert any(e.payload.get("session_id") == session.id for e in events)

        assert daemon.stop_calls == [short_id]

    # ------------------------------------------------------------------
    # Issue #176 Layer 1: headless-session backstop tests
    # ------------------------------------------------------------------

    def _write_headless_context(
        self,
        worktree: Path,
        *,
        session_id: str,
        ticket_id: str | None = SEED_TICKET_ID,
    ) -> None:
        """Write a cw-context.json with ``headless: true`` (dispatch-spawned)."""
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "session_name": "test-client/auto-dev/137",
                    "client": "test-client",
                    "purpose": "impl",
                    "ticket_id": ticket_id,
                    "headless": True,
                }
            )
        )

    def test_signal_stop_timed_out_when_no_sentinel_after_budget(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Headless session past budget with no sentinel → TIMED_OUT + event.

        This is the primary regression test for GitHub issue #176 Layer 1:
        a session that orphaned (Stop fires, no sentinel, bg_tasks empty)
        must NOT silently transition to COMPLETED.
        """
        # Seed session started 61 minutes ago (past the 60-min budget).
        import datetime as dt

        from cw.cli import HEADLESS_TIMEOUT_SECONDS
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        started_at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        hook_time = dt.datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)
        assert (hook_time - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree-timed-out"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-timed-out",
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=started_at,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)

        # Seed a RUNNING TicketTask so we can verify it reverts to PENDING.
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                )
            ]
        )
        save_dev_queue(dev_store)

        self._write_headless_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-timed-out",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
                "last_assistant_message": "Waiting for review agents to complete...",
            }
        )
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.TIMED_OUT
        assert updated.claude_session_id == "claude-uuid-timed-out"

        # SESSION_TIMED_OUT event emitted with expected payload fields.
        events = read_events(
            consumer="test-timed-out",
            event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] == session.id
        assert payload["ticket_id"] == self.SEED_TICKET_ID
        assert payload["elapsed_seconds"] >= HEADLESS_TIMEOUT_SECONDS
        assert "Waiting for review agents" in str(
            payload.get("last_assistant_message_excerpt", "")
        )

        # SESSION_COMPLETED must NOT have been emitted.
        completed_events = read_events(
            consumer="test-timed-out-no-completed",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(
            e.payload.get("session_id") == session.id for e in completed_events
        )

        # TicketTask must have been reverted to PENDING.
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

        # native_daemon.stop called to clean up the daemon worker.
        assert daemon.stop_calls == [short_id]

    def test_signal_stop_completes_normally_with_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Headless session with sentinel present → COMPLETED as normal.

        Verifies that the backstop does not interfere with successful runs:
        when the sentinel block is present in the transcript, the session
        should complete normally even if the budget has elapsed.
        See GitHub issue #176 Layer 1.
        """
        import datetime as dt

        from cw.native_daemon import FakeNativeDaemonClient

        started_at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        hook_time = dt.datetime(2026, 1, 1, 0, 31, 0, tzinfo=UTC)

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree-sentinel"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-sentinel",
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=started_at,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        self._write_headless_context(worktree, session_id=session.id)

        # Write a real Claude-shaped transcript JSONL with an assistant
        # record carrying a parseable sentinel block. The parser walks the
        # same shape signal_stop sees in production (issue #176 Layer 1).
        claude_session_id = "abc12345"
        fake_home = tmp_path / "fake-home"
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True, exist_ok=True)
        # Use the preserved #215 fixture — it parses to a valid AutoDevResult
        # with status=plan_pending_approval and exercises every cross-field
        # invariant in auto_dev_result.py.
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _SENTINEL_215_PLAN_PENDING}],
            },
        }
        (transcript_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED

        timed_out_events = read_events(
            consumer="test-sentinel-no-timeout",
            event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
        )
        assert not any(
            e.payload.get("session_id") == session.id for e in timed_out_events
        )

    def test_signal_stop_populates_last_result_on_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Headless session with a parseable sentinel → session.last_result set.

        Regression for GitHub issue #225: signal_stop previously called
        the bool sentinel-check helper and threw the parse away. Headless
        DAEMON sessions completed with last_result=None even when the
        agent emitted a valid AUTO_DEV_RESULT block. The orchestrator's
        consume_completed_sessions then had nothing to route on.
        """
        import datetime as dt

        from cw.native_daemon import FakeNativeDaemonClient

        started_at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        hook_time = dt.datetime(2026, 1, 1, 0, 31, 0, tzinfo=UTC)

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree-225"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-225",
            name="test-client/auto-dev/214",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=started_at,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        self._write_headless_context(worktree, session_id=session.id)

        # Write a real transcript with the preserved #214 sentinel — the
        # exact failure mode that motivated #225.
        claude_session_id = "uuid-225"
        fake_home = tmp_path / "fake-home"
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _SENTINEL_214_BLOCKED}],
            },
        }
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.last_result is not None
        assert updated.last_result["status"] == "blocked"
        assert updated.last_result["ticket_id"] == "214"
        assert updated.last_result["stage_reached"] == "stage1_plan"

    def test_signal_stop_defers_under_budget_no_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Headless session under budget with no sentinel → defer (no transition).

        The session remains ACTIVE; no event is emitted. Another Stop hook
        will fire later, or reconcile will catch it.
        See GitHub issue #176 Layer 1.
        """
        import datetime as dt

        from cw.native_daemon import FakeNativeDaemonClient

        # Session is 5 minutes old — well under the 30-minute budget.
        started_at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree-under-budget"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-under-budget",
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=started_at,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        self._write_headless_context(worktree, session_id=session.id)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-under-budget",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        # Session must not have changed status.
        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.ACTIVE

        # No events of either kind.
        for event_type in (
            OrchestratorEventType.SESSION_COMPLETED,
            OrchestratorEventType.SESSION_TIMED_OUT,
        ):
            events = read_events(
                consumer=f"test-under-budget-{event_type}",
                event_types=[event_type],
            )
            assert not any(e.payload.get("session_id") == session.id for e in events)

        # daemon.stop must NOT have been called.
        assert daemon.stop_calls == []

    def _write_transcript(
        self,
        worktree: Path,
        claude_session_id: str,
        assistant_text: str,
        home: Path,
    ) -> None:
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )

    def _setup_headless_session(
        self,
        tmp_path: Path,
        session_id: str,
        worktree_name: str,
    ) -> tuple[Path, Session]:
        """Seed state with a DAEMON ACTIVE session and return (worktree, session)."""
        import datetime as dt

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / worktree_name
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id=session_id,
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        return worktree, session

    def test_signal_stop_no_op_marks_task_completed_directly(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """no_op sentinel → task COMPLETED before session COMPLETED.

        Regression for GitHub issue #251 Bug A: _apply_sentinel_to_task
        must set the task to COMPLETED *before* save_state marks the
        session COMPLETED, closing the race with revert_completed_silent_tasks.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-251-no-op", "worktree-251-no-op"
        )
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    attempts=1,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session.id)

        claude_session_id = "uuid-251-no-op"
        fake_home = tmp_path / "fake-home-no-op"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_251_NO_OP, fake_home
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.COMPLETED

    def test_signal_stop_validation_failed_reverts_to_pending_under_cap(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """validation_failed sentinel with attempts < cap → task PENDING.

        Regression for GitHub issue #251 Bug B: malformed sentinel with
        reason='validation_failed' should revert to PENDING (retry) when
        the task has not yet exceeded the hard cap.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-251-vf-under", "worktree-251-vf-under"
        )
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    attempts=1,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session.id)

        claude_session_id = "uuid-251-vf-under"
        fake_home = tmp_path / "fake-home-vf-under"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_251_VALIDATION_FAILED, fake_home
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

    def test_signal_stop_validation_failed_marks_failed_at_cap(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """validation_failed sentinel with attempts >= cap → task FAILED.

        Regression for GitHub issue #251 Bug B: once a task hits the hard
        cap (_VALIDATION_FAILED_MAX_ATTEMPTS=3) on validation_failed retries,
        it must be marked FAILED to stop infinite re-dispatch.
        """
        import datetime as dt

        from cw.cli import _VALIDATION_FAILED_MAX_ATTEMPTS
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-251-vf-cap", "worktree-251-vf-cap"
        )
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    attempts=_VALIDATION_FAILED_MAX_ATTEMPTS,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session.id)

        claude_session_id = "uuid-251-vf-cap"
        fake_home = tmp_path / "fake-home-vf-cap"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_251_VALIDATION_FAILED, fake_home
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.FAILED

    def test_signal_stop_blocked_retry_eligible_reverts_to_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked + retry_eligible=True → task PENDING + session_id cleared.

        The orchestrator should re-dispatch the ticket on the next tick.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-251-blocked-retry", "worktree-251-blocked-retry"
        )
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    attempts=1,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session.id)

        claude_session_id = "uuid-251-blocked-retry"
        fake_home = tmp_path / "fake-home-blocked-retry"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_251_BLOCKED_RETRY, fake_home
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

    def test_signal_stop_blocked_not_retry_eligible_marks_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked sentinel with retry_eligible=False → task COMPLETED (needs human).

        A non-retryable blocker signals that human intervention is required;
        the task must not re-enter the dispatch queue automatically.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-251-blocked-no-retry", "worktree-251-blocked-no-retry"
        )
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    attempts=1,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session.id)

        claude_session_id = "uuid-251-blocked-no-retry"
        fake_home = tmp_path / "fake-home-blocked-no-retry"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_251_BLOCKED_NO_RETRY, fake_home
        )
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.COMPLETED


class TestSentinelPresentInTranscript:
    """Direct tests for the real _sentinel_present_in_transcript helper.

    Earlier tests mocked this helper to keep TestSignalStop tightly scoped,
    which let the original implementation ship with a bug: it ran
    ``extract_block`` against the raw JSONL file text, but Claude transcripts
    JSON-encode each ``message.content[].text`` field — so the sentinel's
    newlines appear as the literal two-character sequence ``\\n``, and
    ``extract_block`` (which expects real newlines) misses every real run.

    These tests exercise the helper end-to-end against transcripts shaped
    the way Claude actually writes them, so the regression cannot recur.
    See GitHub issue #176 Layer 1.
    """

    def _write_transcript(
        self,
        worktree: Path,
        claude_session_id: str,
        assistant_text: str,
        home: Path,
    ) -> None:
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )

    def test_returns_true_when_sentinel_embedded_in_jsonl_assistant_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real failure mode: sentinel JSON-escaped inside an assistant text block.

        Regression for issue #176 — the original helper read the file as
        raw text and missed sentinels whose newlines had been encoded as
        ``\\n``. Decoding each assistant text block before scanning fixes it.
        """
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-170"
        worktree.mkdir(parents=True)
        sentinel_text = (
            "Comment posted. Emitting result.\n\n"
            "```\n"
            "<<<AUTO_DEV_RESULT\n"
            '{"schema_version": 2, "ticket_id": "170", "status": "shipped"}\n'
            "AUTO_DEV_RESULT>>>\n"
            "```\n"
        )
        self._write_transcript(worktree, "uuid-with-sentinel", sentinel_text, fake_home)

        assert _sentinel_present_in_transcript(str(worktree), "uuid-with-sentinel")

    def test_returns_false_when_no_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-200"
        worktree.mkdir(parents=True)
        self._write_transcript(
            worktree, "uuid-empty", "Plain status update, no sentinel here.", fake_home
        )

        assert not _sentinel_present_in_transcript(str(worktree), "uuid-empty")

    def test_returns_false_when_transcript_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-201"
        worktree.mkdir(parents=True)

        assert not _sentinel_present_in_transcript(str(worktree), "missing-uuid")

    def test_returns_false_when_session_id_is_none(
        self,
        tmp_path: Path,
    ) -> None:
        from cw.cli import _sentinel_present_in_transcript

        assert not _sentinel_present_in_transcript(str(tmp_path), None)

    def test_skips_non_assistant_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sentinel string buried in a user/system event must not count.

        Only the assistant's own output counts as a real sentinel emission.
        """
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-202"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)
        # Sentinel lives inside a user-typed text block — must not count.
        user_record = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<<<AUTO_DEV_RESULT\n"
                            '{"schema_version": 2, "status": "shipped"}\n'
                            "AUTO_DEV_RESULT>>>"
                        ),
                    }
                ],
            },
        }
        path = project_dir / "uuid-user-only.jsonl"
        path.write_text(json.dumps(user_record) + "\n")

        assert not _sentinel_present_in_transcript(str(worktree), "uuid-user-only")

    def test_tolerates_malformed_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bad JSON lines are skipped; subsequent valid lines still scanned."""
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-203"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)
        valid_record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<<<AUTO_DEV_RESULT\n"
                            '{"schema_version": 2, "status": "shipped"}\n'
                            "AUTO_DEV_RESULT>>>"
                        ),
                    }
                ],
            },
        }
        (project_dir / "uuid-mixed.jsonl").write_text(
            "not json at all\n"
            + json.dumps({"type": "system"})
            + "\n"
            + json.dumps(valid_record)
            + "\n"
        )

        assert _sentinel_present_in_transcript(str(worktree), "uuid-mixed")


# Real sentinel payloads preserved from the 2026-05-24 dogfood wave (see #225).
# Embedded inline so the regression tests survive worktree cleanup. Both blocks
# parse cleanly under the current invariants; their presence as fixtures locks
# in the round-trip through _parse_sentinel_from_transcript.
_SENTINEL_214_BLOCKED = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 1,\n'
    '  "ticket_id": "214",\n'
    '  "status": "blocked",\n'
    '  "stage_reached": "stage1_plan",\n'
    '  "scope": {"tier": "small", "files": 0, "lines_estimate": 0, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "EXIT_FOR_HUMAN_REVIEW", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": {"stage": "stage1_plan", "reason": "agent_block", '
    '"details": "cross-repo scope"},\n'
    '  "next_actions": []\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

_SENTINEL_215_PLAN_PENDING = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "215",\n'
    '  "status": "plan_pending_approval",\n'
    '  "stage_reached": "stage1_plan",\n'
    '  "scope": {"tier": "large", "files": 11, "lines_estimate": 626, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "generated",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "next_actions": ["user_approve_plan"]\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

# Sentinel fixtures for GitHub issue #251 tests.  Same structural shape as
# the #214/#215 pairs above; ticket_id "137" matches TestSignalStop.SEED_TICKET_ID.
_SENTINEL_251_NO_OP = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "no_op",\n'
    '  "stage_reached": "stage1_pre_flight",\n'
    '  "scope": {"tier": "small", "files": 0, "lines_estimate": 0, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "next_actions": ["close_issue_as_completed"]\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

_SENTINEL_251_VALIDATION_FAILED = (
    # Valid JSON that fails Pydantic cross-field validation: status=shipped
    # requires next_actions=["wait_for_ci"] but the list is empty.  Produces
    # BlockedResult(reason="validation_failed") from parse_stdout §6 (5).
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "shipped",\n'
    '  "stage_reached": "stage5_post_create",\n'
    '  "scope": {"tier": "small", "files": 1, "lines_estimate": 10, '
    '"lines_actual": 5, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": "auto-dev/137",\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": "abc123",\n'
    '  "commits": ["def456"],\n'
    '  "pr": {"number": 1, '
    '"url": "https://github.com/foo/bar/pull/1", '
    '"auto_merge": true, "base": "main"},\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "next_actions": []\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

_SENTINEL_251_BLOCKED_RETRY = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "blocked",\n'
    '  "stage_reached": "stage1_pre_flight",\n'
    '  "scope": {"tier": "small", "files": 0, "lines_estimate": 0, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": {"stage": "pre_flight", '
    '"reason": "local_main_diverged_from_origin", '
    '"details": "local_main=abc, origin_main=def, ahead=1, behind=0", '
    '"retry_eligible": true, "retry_delay_seconds": null},\n'
    '  "next_actions": ["sync_local_main"]\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

_SENTINEL_251_BLOCKED_NO_RETRY = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "blocked",\n'
    '  "stage_reached": "stage1_plan",\n'
    '  "scope": {"tier": "small", "files": 0, "lines_estimate": 0, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "EXIT_FOR_HUMAN_REVIEW", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": {"stage": "stage1_plan", "reason": "plan_unreviewable", '
    '"details": "MUST_FIX persists after revision", '
    '"retry_eligible": false, "retry_delay_seconds": null},\n'
    '  "next_actions": []\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)


class TestParseSentinelFromTranscript:
    """Tests for _parse_sentinel_from_transcript (GitHub issue #225).

    Headless DAEMON sessions complete via signal_stop, not the cw wrapper.
    The wrapper's signal_completed is the only path that assigns
    session.last_result today, so headless sessions emit valid sentinels
    that the orchestrator never sees. This helper walks the same Claude
    transcript JSONL the bool checker uses, but on a sentinel hit it
    returns the parsed AutoDevResult (or BlockedResult for malformed
    blocks) instead of throwing the parse away.
    """

    def _write_transcript(
        self,
        worktree: Path,
        claude_session_id: str,
        assistant_text: str,
        home: Path,
    ) -> None:
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )

    def test_returns_auto_dev_result_on_clean_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clean sentinel block → AutoDevResult instance, not bool or BlockedResult."""
        from cw.auto_dev_result import AutoDevResult
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-215"
        worktree.mkdir(parents=True)
        self._write_transcript(
            worktree, "uuid-215", _SENTINEL_215_PLAN_PENDING, fake_home
        )

        parsed = _parse_sentinel_from_transcript(str(worktree), "uuid-215")
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert parsed.ticket_id == "215"

    def test_returns_auto_dev_result_for_blocked_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression fixture: real #214 transcript shape parses cleanly."""
        from cw.auto_dev_result import AutoDevResult
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-214"
        worktree.mkdir(parents=True)
        self._write_transcript(worktree, "uuid-214", _SENTINEL_214_BLOCKED, fake_home)

        parsed = _parse_sentinel_from_transcript(str(worktree), "uuid-214")
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "blocked"
        assert parsed.ticket_id == "214"
        assert parsed.blocker is not None
        assert parsed.blocker.reason == "agent_block"

    def test_returns_blocked_result_when_sentinel_json_invalid(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel framing present but inner JSON malformed → BlockedResult.

        Mirrors parse_stdout's §6 (3) handling: the parser does not raise.
        """
        from cw.auto_dev_result import BlockedResult
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-bad"
        worktree.mkdir(parents=True)
        bad_sentinel = "<<<AUTO_DEV_RESULT\n{this is not valid JSON\nAUTO_DEV_RESULT>>>"
        self._write_transcript(worktree, "uuid-bad", bad_sentinel, fake_home)

        parsed = _parse_sentinel_from_transcript(str(worktree), "uuid-bad")
        assert isinstance(parsed, BlockedResult)
        assert parsed.status == "blocked"

    def test_returns_none_when_no_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No sentinel in transcript → None (distinct from BlockedResult).

        Callers (signal_stop) use None to mean "no result yet, defer or
        time out per budget"; a non-None return means the agent emitted
        something — even an invalid block — and we should capture it.
        """
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-empty"
        worktree.mkdir(parents=True)
        self._write_transcript(
            worktree, "uuid-empty", "Status update with no sentinel.", fake_home
        )

        assert _parse_sentinel_from_transcript(str(worktree), "uuid-empty") is None

    def test_returns_none_when_transcript_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing transcript file → None (fail-open like the bool helper)."""
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-missing"
        worktree.mkdir(parents=True)

        assert _parse_sentinel_from_transcript(str(worktree), "uuid-missing") is None

    def test_returns_none_when_session_id_is_none(
        self,
        tmp_path: Path,
    ) -> None:
        """No claude_session_id → None without touching the filesystem."""
        from cw.cli import _parse_sentinel_from_transcript

        assert _parse_sentinel_from_transcript(str(tmp_path), None) is None

    def test_skips_non_assistant_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel inside a user message must not be captured.

        Mirrors the bool helper's same-block scoping — only assistant text
        emissions count as a real sentinel.
        """
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-userblock"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True)
        user_record = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": _SENTINEL_215_PLAN_PENDING},
                ],
            },
        }
        (project_dir / "uuid-userblock.jsonl").write_text(
            json.dumps(user_record) + "\n"
        )

        assert _parse_sentinel_from_transcript(str(worktree), "uuid-userblock") is None


class TestCompletion:
    def test_completion_command_bash(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["completion", "bash"])
        assert "_CW_COMPLETE=bash_source" in result.output

    def test_completion_command_zsh(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["completion", "zsh"])
        assert "_CW_COMPLETE=zsh_source" in result.output

    def test_completion_command_fish(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["completion", "fish"])
        assert "_CW_COMPLETE=fish_source" in result.output

    def test_completion_command_invalid_shell(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["completion", "powershell"])
        assert result.exit_code != 0

    def test_completion_shows_in_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["completion", "--help"])
        assert result.exit_code == 0
        assert "shell completion" in result.output.lower()


class TestCompleteCallbacks:
    def test_complete_client_matches(
        self,
        tmp_config_dir: Path,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  alpha:\n"
            "    workspace_path: /tmp/a\n"
            "  beta:\n"
            "    workspace_path: /tmp/b\n"
            "  apricot:\n"
            "    workspace_path: /tmp/c\n"
        )

        # None ctx/param are fine - callbacks don't use them
        items = _complete_client(None, None, "a")  # type: ignore[arg-type]
        names = [item.value for item in items]
        assert "alpha" in names
        assert "apricot" in names
        assert "beta" not in names

    def test_complete_client_empty_prefix(
        self,
        tmp_config_dir: Path,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  alpha:\n"
            "    workspace_path: /tmp/a\n"
            "  beta:\n"
            "    workspace_path: /tmp/b\n"
        )

        items = _complete_client(None, None, "")  # type: ignore[arg-type]
        names = [item.value for item in items]
        assert "alpha" in names
        assert "beta" in names

    def test_complete_session_filters_completed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="comp0001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.BACKGROUNDED,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="comp0002",
                    name="test-client/idea",
                    client="test-client",
                    purpose=SessionPurpose.IDEA,
                    status=SessionStatus.COMPLETED,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        items = _complete_session(None, None, "")  # type: ignore[arg-type]
        names = [item.value for item in items]
        assert "test-client/impl" in names
        assert "test-client/idea" not in names

    def test_complete_session_prefix_filter(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="pref0001",
                    name="alpha/impl",
                    client="alpha",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
                Session(
                    id="pref0002",
                    name="beta/impl",
                    client="beta",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        items = _complete_session(None, None, "alpha")  # type: ignore[arg-type]
        names = [item.value for item in items]
        assert "alpha/impl" in names
        assert "beta/impl" not in names


class TestListSessions:
    def test_empty_state(
        self,
        tmp_config_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        save_state(CwState())

        _display_sessions()

        output = capsys.readouterr().out
        assert "No sessions tracked" in output

    def test_filters_completed(
        self,
        tmp_config_dir: Path,
        sample_state: CwState,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        save_state(sample_state)

        _display_sessions()

        output = capsys.readouterr().out
        # Active and backgrounded should appear, completed should not
        assert "sess0001" in output
        assert "sess0002" in output
        assert "sess0003" not in output

    @freeze_time("2025-01-15 12:00:00", tz_offset=0)
    def test_formats_table(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        state = CwState(
            sessions=[
                Session(
                    id="fmt00001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
                )
            ]
        )
        save_state(state)

        _display_sessions()

        output = capsys.readouterr().out
        assert "CLIENT" in output
        assert "PURPOSE" in output
        assert "STATUS" in output
        assert "test-client" in output
        assert "2h ago" in output


class TestShowStatus:
    @freeze_time("2025-01-15 12:00:00", tz_offset=0)
    def test_counts_and_formatting(
        self,
        tmp_config_dir: Path,
        sample_state: CwState,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cw.cmux import FakeCmuxAdapter

        monkeypatch.setattr("cw.cli.get_cmux_adapter", FakeCmuxAdapter)
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  test-client:\n"
            "    workspace_path: /tmp/ws\n"
            "  other-client:\n"
            "    workspace_path: /tmp/ws2\n"
        )

        save_state(sample_state)

        _display_status()

        output = capsys.readouterr().out
        assert "Clients configured: 2" in output
        assert "Active sessions:    1" in output
        assert "Backgrounded:       1" in output

    def test_check_dead_sessions_reaps_phantom_with_surface_ref(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """_check_and_mark_dead_sessions reaps sessions whose surface is gone."""
        from cw.cmux import FakeCmuxAdapter

        def _adapter_with_decoy() -> FakeCmuxAdapter:
            # Non-empty live set prevents reconcile's outage guard from
            # refusing to mutate state (see cw.reconcile._looks_like_backend_outage).
            a = FakeCmuxAdapter()
            a.spawn("decoy-ws", "echo")
            return a

        monkeypatch.setattr("cw.cli.get_cmux_adapter", _adapter_with_decoy)
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="active01",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="impl",
                )
            ]
        )
        save_state(state)

        _display_status()

        output = capsys.readouterr().out
        # Real reconciler detects missing surface — session is reaped
        assert "Reaped phantom session: test-client/impl" in output
        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED


class TestBgNotifyCli:
    def test_bg_with_notify(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg", "--notify", "idea"])
            mock_bg.assert_called_once_with(None, notify="idea", auto=False)

    def test_bg_with_notify_short(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg", "-n", "idea"])
            mock_bg.assert_called_once_with(None, notify="idea", auto=False)


class TestInitCli:
    def test_init_with_args(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("my-repo")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "my-repo", "--path", str(repo)],
        )
        assert result.exit_code == 0, result.output
        assert "Added client 'my-repo'" in result.output
        assert "cw start my-repo" in result.output

    def test_init_with_branch_and_purposes(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("my-repo")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "init",
                "my-repo",
                "--path",
                str(repo),
                "--branch",
                "develop",
                "--purposes",
                "impl,idea",
            ],
        )
        assert result.exit_code == 0, result.output

        clients = load_clients()
        assert "my-repo" in clients
        assert clients["my-repo"].default_branch == "develop"
        assert len(clients["my-repo"].auto_purposes) == 2

    def test_init_interactive(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("my-repo")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init"],
            input=f"my-repo\n{repo}\nmain\n",
        )
        assert result.exit_code == 0, result.output
        assert "Added client 'my-repo'" in result.output

        clients = load_clients()
        assert "my-repo" in clients
        assert clients["my-repo"].workspace_path == repo
        assert clients["my-repo"].default_branch == "main"

    def test_init_missing_path_errors(
        self,
        tmp_config_dir: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init", "my-repo"])
        assert result.exit_code != 0
        assert "Path is required" in result.output

    def test_init_duplicate_errors(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        repo = make_git_repo("my-repo")

        # Add once
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "my-repo", "--path", str(repo)],
        )
        assert result.exit_code == 0

        # Try again — should fail
        result = runner.invoke(
            main,
            ["init", "my-repo", "--path", str(repo)],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output


class TestQueueNextCli:
    def test_next_empty_queue(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.peek_next", return_value=None):
            result = runner.invoke(main, ["queue", "next", "my-client"])
            assert result.exit_code == 0
            assert "No pending items" in result.output

    def test_next_shows_item(self, tmp_config_dir: Path) -> None:

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
                priority=5,
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.peek_next", return_value=item):
            result = runner.invoke(main, ["queue", "next", "my-client"])
            assert result.exit_code == 0
            assert item.id in result.output
            assert "Fix bug" in result.output
            assert "priority=5" in result.output

    def test_next_json_output(self, tmp_config_dir: Path) -> None:

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.peek_next", return_value=item):
            result = runner.invoke(
                main,
                ["queue", "next", "my-client", "--json"],
            )
            assert result.exit_code == 0
            assert '"description": "Fix bug"' in result.output

    def test_next_with_purpose(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.peek_next", return_value=None) as mock_peek:
            runner.invoke(
                main,
                ["queue", "next", "my-client", "--purpose", "impl"],
            )
            mock_peek.assert_called_once_with(
                "my-client",
                purpose=SessionPurpose.IMPL,
            )


class TestQueueClaimCli:
    def test_claim_empty_queue(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.claim_next", return_value=None):
            result = runner.invoke(main, ["queue", "claim", "my-client"])
            assert result.exit_code == 0
            assert "No pending items" in result.output

    def test_claim_shows_item(self, tmp_config_dir: Path) -> None:

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.claim_next", return_value=item):
            result = runner.invoke(main, ["queue", "claim", "my-client"])
            assert result.exit_code == 0
            assert "Claimed:" in result.output
            assert item.id in result.output

    def test_claim_json_output(self, tmp_config_dir: Path) -> None:

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.claim_next", return_value=item):
            result = runner.invoke(
                main,
                ["queue", "claim", "my-client", "--json"],
            )
            assert result.exit_code == 0
            assert '"description": "Fix bug"' in result.output

    def test_claim_by_id(self, tmp_config_dir: Path) -> None:

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.claim_by_id", return_value=item) as mock_claim:
            result = runner.invoke(
                main,
                ["queue", "claim", "my-client", "--id", "abc12345"],
            )
            assert result.exit_code == 0
            mock_claim.assert_called_once_with("my-client", "abc12345")

    def test_claim_with_purpose(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.claim_next", return_value=None) as mock_claim:
            runner.invoke(
                main,
                ["queue", "claim", "my-client", "--purpose", "debt"],
            )
            mock_claim.assert_called_once_with(
                "my-client",
                purpose=SessionPurpose.DEBT,
            )


class TestQueueCompleteCli:
    def test_complete_success(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.complete_item") as mock_complete:
            result = runner.invoke(
                main,
                ["queue", "complete", "my-client", "abc123", "--result", "All done"],
            )
            assert result.exit_code == 0
            assert "Completed: abc123" in result.output
            mock_complete.assert_called_once_with(
                "my-client",
                "abc123",
                "All done",
            )

    def test_complete_default_result(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.complete_item") as mock_complete:
            result = runner.invoke(
                main,
                ["queue", "complete", "my-client", "abc123"],
            )
            assert result.exit_code == 0
            mock_complete.assert_called_once_with(
                "my-client",
                "abc123",
                "",
            )

    def test_complete_not_found(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.complete_item",
            side_effect=ValueError("Queue item not found: bad-id"),
        ):
            result = runner.invoke(
                main,
                ["queue", "complete", "my-client", "bad-id"],
            )
            assert result.exit_code != 0


class TestQueueFailCli:
    def test_fail_success(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.fail_item") as mock_fail:
            result = runner.invoke(
                main,
                ["queue", "fail", "my-client", "abc123", "--error", "Crashed"],
            )
            assert result.exit_code == 0
            assert "Failed: abc123" in result.output
            mock_fail.assert_called_once_with(
                "my-client",
                "abc123",
                "Crashed",
            )

    def test_fail_default_error(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.fail_item") as mock_fail:
            result = runner.invoke(
                main,
                ["queue", "fail", "my-client", "abc123"],
            )
            assert result.exit_code == 0
            mock_fail.assert_called_once_with(
                "my-client",
                "abc123",
                "",
            )

    def test_fail_not_found(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.fail_item",
            side_effect=ValueError("Queue item not found: bad-id"),
        ):
            result = runner.invoke(
                main,
                ["queue", "fail", "my-client", "bad-id"],
            )
            assert result.exit_code != 0


def test_display_status_reconciles_phantom_active_sessions(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cw status` reports and reaps sessions with missing surfaces."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import load_state, save_state
    from cw.models import CwState, Session, SessionPurpose, SessionStatus

    save_state(
        CwState(
            sessions=[
                Session(
                    id="phantom1",
                    name="client-a/impl",
                    client="client-a",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    surface_ref="gone",
                ),
            ]
        )
    )

    # Non-empty live set prevents the outage guard from aborting reconcile;
    # the "gone" surface_ref still isn't in the set so phantom1 is reaped.
    def _adapter_with_decoy() -> FakeCmuxAdapter:
        a = FakeCmuxAdapter()
        a.spawn("decoy-ws", "echo")
        return a

    monkeypatch.setattr("cw.cli.get_cmux_adapter", _adapter_with_decoy)

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Reaped phantom session" in result.output
    assert "client-a/impl" in result.output

    reloaded = load_state()
    reaped = reloaded.find_by_name_or_id("phantom1")
    assert reaped is not None
    assert reaped.status == SessionStatus.COMPLETED
