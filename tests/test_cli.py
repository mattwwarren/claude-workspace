"""Tests for cw.cli - Click CLI dispatcher."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from click.testing import CliRunner, Result
from freezegun import freeze_time

from cw.cli import (
    _complete_client,
    _complete_session,
    _configure_logging,
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
    Stage,
    TaskSpec,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import click
    import pytest

    from cw.models import QueueItemStatus


class TestCli:
    def test_version(self) -> None:
        from cw import __version__

        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_resolve_version_from_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cw

        def _fake(name: str) -> str:
            return "9.9.9"

        monkeypatch.setattr(cw, "version", _fake)
        assert cw._resolve_version() == "9.9.9"

    def test_resolve_version_fallback_when_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from importlib.metadata import PackageNotFoundError

        import cw

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr(cw, "version", _raise)
        assert cw._resolve_version() == "0.0.0+unknown"

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Claude Workspace" in result.output

    def test_start_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.start_session") as mock_start:
            runner.invoke(main, ["start", "my-client"])
            mock_start.assert_called_once_with(
                "my-client",
                "impl",
                worktree=None,
                parent=None,
            )

    def test_start_with_purpose(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.start_session") as mock_start:
            runner.invoke(main, ["start", "--purpose", "idea", "my-client"])
            mock_start.assert_called_once_with(
                "my-client",
                "idea",
                worktree=None,
                parent=None,
            )

    def test_start_with_worktree(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.start_session") as mock_start:
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
        with patch("cw.cli.sessions.start_session") as mock_start:
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
        with patch("cw.cli.sessions.start_session") as mock_start:
            runner.invoke(main, ["start", "my-client"])
            _, kwargs = mock_start.call_args
            assert kwargs.get("parent") is None

    def test_bg_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.background_session") as mock_bg:
            runner.invoke(main, ["bg"])
            mock_bg.assert_called_once_with(None, notify=None, auto=False)

    def test_bg_with_session_name(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.background_session") as mock_bg:
            runner.invoke(main, ["bg", "personal/debt"])
            mock_bg.assert_called_once_with(
                "personal/debt",
                notify=None,
                auto=False,
            )

    def test_resume_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.resume_session") as mock_resume:
            runner.invoke(main, ["resume", "my-session"])
            mock_resume.assert_called_once_with("my-session")

    def test_list_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions._display_sessions") as mock_list:
            runner.invoke(main, ["list"])
            mock_list.assert_called_once()

    def test_status_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions._display_status") as mock_status:
            runner.invoke(main, ["status"])
            mock_status.assert_called_once()

    def test_done_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.done_session") as mock_done:
            runner.invoke(main, ["done", "my-session"])
            mock_done.assert_called_once_with(
                "my-session",
                cleanup=False,
                force=False,
            )

    def test_done_with_cleanup(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.done_session") as mock_done:
            runner.invoke(main, ["done", "my-session", "--cleanup", "--force"])
            mock_done.assert_called_once_with(
                "my-session",
                cleanup=True,
                force=True,
            )

    def test_done_no_session_arg(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.done_session") as mock_done:
            runner.invoke(main, ["done"])
            mock_done.assert_called_once_with(
                None,
                cleanup=False,
                force=False,
            )

    def test_config_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.config_cmds.show_config") as mock_config:
            runner.invoke(main, ["config"])
            mock_config.assert_called_once()

    def test_error_display(self) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.sessions.start_session",
            side_effect=CwError("Test error message"),
        ):
            result = runner.invoke(main, ["start", "bad-client"])
            assert result.exit_code != 0
            assert "Test error message" in result.output

    def test_daemon_once_emits_deprecation_notice(self) -> None:
        """cw daemon --once: banner calls it deprecated, redirects to dev-queue run."""
        runner = CliRunner()
        result = runner.invoke(main, ["daemon", "--once"])
        assert result.exit_code == 0
        assert "deprecated and has no effect" in result.output
        assert "cw dev-queue run" in result.output


class TestLogging:
    """Logging handler configuration at the CLI entrypoint (#423).

    These tests drive ``_configure_logging`` against a root logger whose
    handlers have been cleared to simulate a fresh ``cw`` process. They
    deliberately do NOT rely on global log capture under pytest, and they
    restore the root logger's handlers/level afterward so they never leak
    state into the rest of the suite (the ``force=True`` regression that
    hung the suite).
    """

    @staticmethod
    @contextmanager
    def _fresh_root_logger() -> Iterator[logging.Logger]:
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        root.handlers.clear()
        try:
            yield root
        finally:
            root.handlers.clear()
            root.handlers.extend(saved_handlers)
            root.setLevel(saved_level)

    def test_installs_stderr_handler_by_default(self) -> None:
        with self._fresh_root_logger() as root:
            _configure_logging(0)
            stream_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            ]
            assert stream_handlers, "no stderr handler installed"
            assert root.level == logging.WARNING

    def test_verbose_flag_sets_info_level(self) -> None:
        with self._fresh_root_logger() as root:
            _configure_logging(1)
            assert root.level == logging.INFO

    def test_double_verbose_flag_sets_debug_level(self) -> None:
        with self._fresh_root_logger() as root:
            _configure_logging(2)
            assert root.level == logging.DEBUG

    def test_emitted_warning_reaches_stderr(self) -> None:
        """A WARNING emitted after configuration is written to stderr."""
        with self._fresh_root_logger():
            stream = io.StringIO()
            with patch.object(sys, "stderr", stream):
                _configure_logging(0)
                logging.getLogger("cw.test_sentinel").warning("sentinel-423")
        assert "sentinel-423" in stream.getvalue()

    def test_no_handler_installed_at_import_time(self) -> None:
        """Importing cw.cli must NOT configure logging.

        Checked in a fresh subprocess so the assertion observes a pristine
        interpreter and never mutates this process's ``sys.modules`` / logging
        state. An in-process pop+reimport of ``cw.cli`` corrupts the rest of
        the suite, so isolation via subprocess is required here.
        """
        code = (
            "import logging, cw.cli, sys; "
            "sys.exit(1 if logging.getLogger().handlers else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"cw.cli configured logging at import time: {result.stderr.decode()}"
        )


class TestUpgradeWorkers:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        completed = SimpleNamespace(
            returncode=0, stdout="Respawned 3 workers\n", stderr=""
        )
        with patch(
            "cw.cli.maintenance.subprocess.run", return_value=completed
        ) as mock_run:
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
        with patch("cw.cli.maintenance.subprocess.run", return_value=completed):
            result = runner.invoke(main, ["upgrade-workers"])
            assert result.exit_code == 2
            assert "boom" in result.output

    def test_claude_not_on_path(self) -> None:
        runner = CliRunner()
        with patch(
            "cw.cli.maintenance.subprocess.run",
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
        patch on ``cw.cli.sessions.sys.stdin``, so this case is exercised by calling
        the command's callback directly with a fake stdin instead.
        """

        class _RaisingStdin:
            def read(self) -> str:
                error_msg = "broken pipe"
                raise OSError(error_msg)

        callback = main.commands["signal-stop"].callback
        assert callable(callback)
        with patch("cw.cli.sessions.sys.stdin", _RaisingStdin()):
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

        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-full-uuid",
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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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

        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-daemon-full-uuid",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert updated.claude_session_id == f"{short_id}-daemon-full-uuid"

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

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient
        from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)
        # Ensure world-state check returns "no merged PR" so this test
        # exercises the TIMED_OUT path regardless of real GitHub state (#315).
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-timed-out-full-uuid",
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
        assert updated.claude_session_id == f"{short_id}-timed-out-full-uuid"

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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

    def test_signal_stop_respects_per_ticket_timeout_override(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-ticket headless_timeout_override is honored on the Stop-hook path.

        Session elapsed 4000s — past the 3600s global budget, but within the
        7200s per-ticket override. The hook must defer (session stays ACTIVE).
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        # 4000s elapsed: past the default 3600s global budget, under 7200s override.
        started_at = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        hook_time = dt.datetime(2026, 1, 1, 1, 6, 40, tzinfo=UTC)  # +4000s
        assert (hook_time - started_at).total_seconds() == 4000

        workspace = tmp_path / "ws-override"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "wt-override"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-override",
            name="test-client/auto-dev/GEN-override",
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

        # Seed a RUNNING TicketTask with headless_timeout_override=7200.
        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GEN-override",
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session.id,
                    headless_timeout_override=7200,
                )
            ]
        )
        save_dev_queue(dev_store)

        # Write headless context with our custom ticket_id.
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": session.id,
                    "session_name": session.name,
                    "client": "test-client",
                    "purpose": "impl",
                    "ticket_id": "GEN-override",
                    "headless": True,
                }
            )
        )

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": "claude-uuid-override",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        # 4000s elapsed < 7200s override → should NOT time out → still ACTIVE.
        state_after = load_state()
        sess_after = next(s for s in state_after.sessions if s.id == "sess-override")
        assert sess_after.status == SessionStatus.ACTIVE, (
            f"Expected ACTIVE but got {sess_after.status} — "
            "per-ticket override not honored on Stop-hook path"
        )

        # TicketTask must remain RUNNING (not reverted to PENDING).
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "GEN-override")
        assert task.status == QueueItemStatus.RUNNING

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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

    def test_signal_stop_premises_pending_v2_marks_task_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """premises_pending_verification at schema_version=2 → BLOCKED_ON_USER.

        Regression for GitHub issue #316: schema_version<4 gate caused
        validation_failed BlockedResult → retry loop. After fix, parses
        as AutoDevResult → BLOCKED_ON_USER (not COMPLETED). See #489.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-316-premises", "worktree-316-premises"
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

        claude_session_id = "uuid-316-premises"
        fake_home = tmp_path / "fake-home-316-premises"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_316_PREMISES_PENDING_V2, fake_home
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_signal_stop_ambiguities_pending_v2_marks_task_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ambiguities_pending_resolution at schema_version=2 → BLOCKED_ON_USER.

        Regression for GitHub issue #316. Paused sentinels must not be
        routed to COMPLETED — they require human attention. See #489.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-316-ambiguities", "worktree-316-ambiguities"
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

        claude_session_id = "uuid-316-ambiguities"
        fake_home = tmp_path / "fake-home-316-ambiguities"
        self._write_transcript(
            worktree, claude_session_id, _SENTINEL_316_AMBIGUITIES_PENDING_V2, fake_home
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient
        from cw.reconcile import _VALIDATION_FAILED_MAX_ATTEMPTS

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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

    def test_signal_stop_blocked_retry_eligible_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked + retry_eligible=True → task BLOCKED_ON_USER (B2 Rule 5).

        Pre-B2 the monolith routing used retry_eligible to decide PENDING vs
        COMPLETED; B2 unifies the routing table and treats blocked as a
        STAGE_FAILURE → BLOCKED_ON_USER regardless of retry_eligible (#698).
        The operator (or the orchestrate lane) decides whether to re-dispatch.
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        # B2: blocked → STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER, not PENDING
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_signal_stop_blocked_not_retry_eligible_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked sentinel with retry_eligible=False → BLOCKED_ON_USER (B2 Rule 5).

        Pre-B2 the monolith routing mapped retry_eligible=False to COMPLETED.
        B2 unifies the routing table: blocked is a STAGE_FAILURE (Rule 5) →
        BLOCKED_ON_USER regardless of retry_eligible (#698). The operator
        decides what to do with a non-retryable blocker.
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        # B2: blocked → STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER, not COMPLETED
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_signal_stop_stale_hook_guard_drops_mismatched_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale stop hook whose claude_session_id doesn't match surface_ref is dropped.

        Regression for GitHub issue #285. When dispatch rewrites cw-context.json
        with session2's ID for a retry, a Stop hook fired by session1's still-live
        Claude process carries session1's Claude UUID in its payload but reads
        session2's CW session ID from context. The stale-hook guard must detect
        the mismatch (payload UUID doesn't start with session2's surface_ref) and
        return without modifying the task or session2's status.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        # Session2 is the current (retry) attempt; surface_ref="next0002".
        worktree, session2 = self._setup_headless_session(
            tmp_path, "sess-285-guard-s2", "worktree-285-guard"
        )
        state = load_state()
        s2 = next(s for s in state.sessions if s.id == session2.id)
        s2.surface_ref = "next0002"
        save_state(state)

        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session2.id,
                    attempts=2,
                )
            ]
        )
        save_dev_queue(dev_store)
        # cw-context.json points to session2 (dispatch overwrote it for retry).
        self._write_headless_context(worktree, session_id=session2.id)

        # Stale hook from session1: UUID starts with "prev0001", not "next0002".
        stale_claude_id = "prev0001-old-session1-uuid"
        fake_home = tmp_path / "fake-home-285-guard"
        self._write_transcript(
            worktree, stale_claude_id, _SENTINEL_251_BLOCKED_RETRY, fake_home
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": stale_claude_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        hook_time = dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        runner = CliRunner()
        with freeze_time(hook_time):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        # Stale hook dropped — task still RUNNING for session2.
        task = next(
            t for t in load_dev_queue().tasks if t.ticket_id == self.SEED_TICKET_ID
        )
        assert task.status == QueueItemStatus.RUNNING
        assert task.session_id == session2.id

        # Session2 still ACTIVE — stale hook must not have completed it.
        s2_after = next(s for s in load_state().sessions if s.id == session2.id)
        assert s2_after.status == SessionStatus.ACTIVE

    def _seed_seq_session(
        self,
        *,
        session_id: str,
        surface_ref: str,
        started_at: datetime,
        workspace: Path,
        worktree: Path,
    ) -> Session:
        """Append a DAEMON ACTIVE session on a shared worktree to state and persist."""
        session = Session(
            id=session_id,
            name="test-client/auto-dev/137",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            surface_ref=surface_ref,
            started_at=started_at,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        return session

    def _invoke_signal_stop(
        self,
        runner: CliRunner,
        *,
        claude_id: str,
        worktree: Path,
        frozen_at: datetime,
    ) -> Result:
        """Invoke ``cw signal-stop`` with a Stop-hook payload at a frozen time."""
        with freeze_time(frozen_at):
            return runner.invoke(
                main,
                ["signal-stop"],
                input=json.dumps(
                    {
                        "session_id": claude_id,
                        "cwd": str(worktree),
                        "hook_event_name": "Stop",
                    }
                ),
            )

    def test_signal_stop_blocked_retry_shipped_sequence_completes_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocked→retry→shipped: task must end at COMPLETED, not PENDING.

        Regression test for GitHub issue #285 (evidence from #139). The pre-fix
        failure sequence:
          1. Attempt 1 blocks (retry_eligible). signal_stop reverts task to
             PENDING and calls native_daemon.stop(session1.surface_ref).
          2. Dispatch re-claims: task RUNNING+session2.id. spawn_create_impl
             overwrites cw-context.json with session2's CW ID.
          3. Stale stop hook fires for session1 AFTER the overwrite. Hook
             payload has session1's Claude UUID; cw-context reads session2's
             CW ID. Without the guard: blocked sentinel applies to session2's
             task → PENDING again; session2 marked COMPLETED prematurely.
          4. Session2 ships but its real stop hook sees session2.status==COMPLETED
             and returns early. Task stuck at PENDING.
        With the stale-hook guard step 3 is a no-op, and session2's shipped
        hook correctly transitions the task to COMPLETED.
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        workspace = tmp_path / "workspace-285"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree-285-seq"
        worktree.mkdir(parents=True, exist_ok=True)

        # === Attempt 1: session1 runs, emits blocked+retry_eligible ===
        session1 = self._seed_seq_session(
            session_id="sess-285-seq-s1",
            surface_ref="aabb1100",
            started_at=dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            workspace=workspace,
            worktree=worktree,
        )

        dev_store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=self.SEED_TICKET_ID,
                    client="test-client",
                    status=QueueItemStatus.RUNNING,
                    session_id=session1.id,
                    attempts=1,
                )
            ]
        )
        save_dev_queue(dev_store)
        self._write_headless_context(worktree, session_id=session1.id)

        s1_claude_id = "aabb1100-session1-full-uuid"
        fake_home = tmp_path / "fake-home-285-seq"
        self._write_transcript(
            worktree, s1_claude_id, _SENTINEL_251_BLOCKED_RETRY, fake_home
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        r1 = self._invoke_signal_stop(
            runner,
            claude_id=s1_claude_id,
            worktree=worktree,
            frozen_at=dt.datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
        )
        assert r1.exit_code == 0, r1.output

        task_after_1 = next(
            t for t in load_dev_queue().tasks if t.ticket_id == self.SEED_TICKET_ID
        )
        # B2: blocked → STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER; session_id retained
        assert task_after_1.status == QueueItemStatus.BLOCKED_ON_USER

        # === Dispatch re-claims: session2 spawned, cw-context.json overwritten ===
        session2 = self._seed_seq_session(
            session_id="sess-285-seq-s2",
            surface_ref="ccdd2200",
            started_at=dt.datetime(2026, 1, 1, 0, 6, 0, tzinfo=UTC),
            workspace=workspace,
            worktree=worktree,
        )

        # B2: apply_staged_decision needs the pipeline to route shipped → COMPLETED.
        # Place task at terminal stage (FINALIZE) so shipped there → COMPLETED.
        _write_staged_clients_yaml_for_test(tmp_config_dir, "test-client")
        store2 = load_dev_queue()
        task2 = next(t for t in store2.tasks if t.ticket_id == self.SEED_TICKET_ID)
        task2.status = QueueItemStatus.RUNNING
        task2.session_id = session2.id
        task2.stage = Stage.FINALIZE  # terminal; shipped here → COMPLETED
        task2.attempts = 2
        save_dev_queue(store2)

        # spawn_create_impl overwrites cw-context.json with session2's CW ID.
        self._write_headless_context(worktree, session_id=session2.id)

        # === Stale hook fires for session1 after cw-context.json overwrite ===
        # Hook payload still carries session1's Claude UUID ("aabb1100-…");
        # cw-context.json now has session2's CW ID. Guard must drop this.
        r_stale = self._invoke_signal_stop(
            runner,
            claude_id=s1_claude_id,
            worktree=worktree,
            frozen_at=dt.datetime(2026, 1, 1, 0, 6, 1, tzinfo=UTC),
        )
        assert r_stale.exit_code == 0, r_stale.output

        task_after_stale = next(
            t for t in load_dev_queue().tasks if t.ticket_id == self.SEED_TICKET_ID
        )
        assert task_after_stale.status == QueueItemStatus.RUNNING, (
            "Stale hook must not have reverted the task to PENDING"
        )
        assert task_after_stale.session_id == session2.id
        s2_mid = next(s for s in load_state().sessions if s.id == session2.id)
        assert s2_mid.status == SessionStatus.ACTIVE, (
            "Stale hook must not have completed session2 prematurely"
        )

        # === Attempt 2: session2 ships ===
        s2_claude_id = "ccdd2200-session2-full-uuid"
        self._write_transcript(worktree, s2_claude_id, _SENTINEL_285_SHIPPED, fake_home)

        r2 = self._invoke_signal_stop(
            runner,
            claude_id=s2_claude_id,
            worktree=worktree,
            frozen_at=dt.datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC),
        )
        assert r2.exit_code == 0, r2.output

        task_final = next(
            t for t in load_dev_queue().tasks if t.ticket_id == self.SEED_TICKET_ID
        )
        assert task_final.status == QueueItemStatus.COMPLETED, (
            f"Expected COMPLETED but got {task_final.status} — "
            "regression: stale stop hook left task PENDING after blocked→retry→shipped"
        )

    def test_signal_stop_schema_version_unsupported_marks_failed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """schema_version_unsupported sentinel → task FAILED on first occurrence.

        Regression for GitHub issue #263 Bug A: a BlockedResult with
        reason='schema_version_unsupported' is a deterministic failure —
        retrying will not produce a different schema_version in the running
        parser binary.  Must be routed to FAILED (not PENDING).
        """
        import datetime as dt

        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._setup_headless_session(
            tmp_path, "sess-263-schema-unsupported", "worktree-263-schema-unsupported"
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

        claude_session_id = "uuid-263-schema-unsupported"
        fake_home = tmp_path / "fake-home-263-schema-unsupported"
        self._write_transcript(
            worktree,
            claude_session_id,
            _SENTINEL_263_SCHEMA_VERSION_UNSUPPORTED,
            fake_home,
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

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
        assert task.status == QueueItemStatus.FAILED

    def test_signal_stop_unknown_blocker_reason_marks_failed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Unknown blocker reason → task FAILED (terminal, but never false success).

        #263 Bug A: unrecognised BlockedResult reasons must not perpetually
        re-dispatch via PENDING — they need a TERMINAL state. #750: that terminal
        state must NOT be COMPLETED — a BlockedResult carries no success signal,
        and COMPLETED silently retires unshipped work as "shipped" (the #728
        loss). FAILED satisfies both: terminal (no re-burn) and honest
        (operator-visible, not a phantom completion).
        """
        from cw.auto_dev_result import BlockedResult, Blocker
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.reconcile import _apply_sentinel_to_task

        _worktree, session = self._setup_headless_session(
            tmp_path, "sess-263-unknown-reason", "worktree-263-unknown-reason"
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

        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="unknown_reason_xyz",
                details="test: unrecognised reason code",
            )
        )
        _apply_sentinel_to_task(self.SEED_TICKET_ID, session.id, sentinel)

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.FAILED


class TestWorldStateCompletionInference:
    """Tests for #315 — world-state check before declaring session.timed_out.

    Acceptance cases:
    (a) PR merged + sentinel missing → inferred COMPLETED
    (b) PR merged + sentinel present → worker sentinel wins (normal COMPLETED)
    (c) branch deleted after merge → inferred COMPLETED (same gh path as (a))
    (d) no PR + no sentinel → timed_out unchanged
    """

    SEED_TICKET_ID = "300"
    _BUDGET_EXCEEDED_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    _BUDGET_EXCEEDED_HOOK_TIME = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)  # 61m

    def _make_impl_session_past_budget(
        self,
        tmp_path: Path,
        session_id: str,
    ) -> tuple[Path, Session]:
        """Seed an IMPL DAEMON session started past the 60-min budget."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / f"wt-{session_id}"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id=session_id,
            name=f"test-client/auto-dev/{self.SEED_TICKET_ID}",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=self._BUDGET_EXCEEDED_START,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)

        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "session_name": session.name,
                    "client": "test-client",
                    "purpose": "impl",
                    "ticket_id": self.SEED_TICKET_ID,
                    "headless": True,
                }
            )
        )
        return worktree, session

    def test_pr_merged_no_sentinel_inferred_completed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cases (a)+(c): PR merged, no sentinel, budget exceeded → inferred COMPLETED.

        Covers both the issue-link-merged path and the branch-deleted-post-merge
        path — from cw's perspective the gh call returns (True, True) in either
        case. The session must be COMPLETED (not TIMED_OUT) and the TicketTask
        must be COMPLETED (not reverted to PENDING). See GitHub #315.
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient
        from cw.reconcile import HEADLESS_TIMEOUT_SECONDS

        assert (
            self._BUDGET_EXCEEDED_HOOK_TIME - self._BUDGET_EXCEEDED_START
        ).total_seconds() > HEADLESS_TIMEOUT_SECONDS

        worktree, session = self._make_impl_session_past_budget(
            tmp_path, "sess-315-inferred"
        )

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

        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-315-uuid",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(self._BUDGET_EXCEEDED_HOOK_TIME):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert isinstance(updated.last_result, dict)
        assert updated.last_result.get("completion_source") == "world_state_inference"

        # SESSION_COMPLETED_INFERRED emitted (not SESSION_TIMED_OUT).
        inferred_events = read_events(
            consumer="test-315-inferred",
            event_types=[OrchestratorEventType.SESSION_COMPLETED_INFERRED],
        )
        assert any(e.payload.get("session_id") == session.id for e in inferred_events)
        matching = next(
            e for e in inferred_events if e.payload.get("session_id") == session.id
        )
        assert matching.payload["ticket_id"] == self.SEED_TICKET_ID
        assert matching.payload["completion_source"] == "world_state_inference"

        timed_out_events = read_events(
            consumer="test-315-no-timed-out",
            event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
        )
        assert not any(
            e.payload.get("session_id") == session.id for e in timed_out_events
        )

        # TicketTask must be COMPLETED (not reverted to PENDING).
        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.COMPLETED
        assert task.session_id is None

        # Daemon surface stopped.
        assert short_id in daemon.stop_calls

    def test_sentinel_wins_when_present_world_state_not_consulted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Case (b): sentinel found → normal COMPLETED path, world-state never queried.

        Even if the PR merged, a present sentinel is the authoritative signal.
        The world-state check lives only in the no-sentinel branch; this test
        verifies pr_is_merged_for_ticket is never called when a sentinel exists.
        See GitHub #315.
        """
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._make_impl_session_past_budget(
            tmp_path, "sess-315-sentinel-wins"
        )

        # Write a real Claude-shaped transcript with a parseable sentinel block.
        claude_session_id = "abc12345"
        fake_home = tmp_path / "fake-home"
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True, exist_ok=True)
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        gh_called = []
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_args, **_kw: gh_called.append(True) or (True, True),
        )

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        hook_stdin = json.dumps(
            {
                "session_id": claude_session_id,
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(self._BUDGET_EXCEEDED_HOOK_TIME):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        # Session completed normally via sentinel — world-state path never fired.
        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.COMPLETED
        assert not gh_called, "gh not consulted when sentinel present"

        inferred_events = read_events(
            consumer="test-315-sentinel-no-inferred",
            event_types=[OrchestratorEventType.SESSION_COMPLETED_INFERRED],
        )
        assert not any(
            e.payload.get("session_id") == session.id for e in inferred_events
        )

    def test_timed_out_when_no_pr_no_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Case (d): no PR + no sentinel + budget exceeded → SESSION_TIMED_OUT.

        When pr_is_merged_for_ticket returns False (no merged PR), behavior is
        identical to pre-#315: session → TIMED_OUT, task → PENDING.
        """
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        worktree, session = self._make_impl_session_past_budget(
            tmp_path, "sess-315-no-pr"
        )

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

        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-315-no-pr-uuid",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(self._BUDGET_EXCEEDED_HOOK_TIME):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.TIMED_OUT

        timed_out_events = read_events(
            consumer="test-315-d-timed-out",
            event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
        )
        assert any(e.payload.get("session_id") == session.id for e in timed_out_events)

        inferred_events = read_events(
            consumer="test-315-d-no-inferred",
            event_types=[OrchestratorEventType.SESSION_COMPLETED_INFERRED],
        )
        assert not any(
            e.payload.get("session_id") == session.id for e in inferred_events
        )

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == self.SEED_TICKET_ID)
        assert task.status == QueueItemStatus.PENDING

    def test_world_state_skipped_for_non_impl_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-IMPL session (purpose=idea) never consults world state.

        World-state inference is gated on purpose==IMPL (the only purpose that
        opens PRs). Other purposes fall through to timed_out unchanged. See #315.
        """
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "wt-315-idea"
        worktree.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="sess-315-idea",
            name=f"test-client/auto-dev/{self.SEED_TICKET_ID}",
            client="test-client",
            purpose=SessionPurpose.IDEA,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            worktree_path=worktree,
            status=SessionStatus.ACTIVE,
            started_at=self._BUDGET_EXCEEDED_START,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)

        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps(
                {
                    "session_id": session.id,
                    "session_name": session.name,
                    "client": "test-client",
                    "purpose": "idea",
                    "ticket_id": self.SEED_TICKET_ID,
                    "headless": True,
                }
            )
        )

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

        daemon = FakeNativeDaemonClient()
        short_id = daemon.spawn_bg(cwd=worktree, prompt="seed")
        state = load_state()
        target = next(s for s in state.sessions if s.id == session.id)
        target.surface_ref = short_id
        save_state(state)
        monkeypatch.setattr("cw.cli.sessions.get_native_daemon_client", lambda: daemon)

        gh_called = []
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda *_args, **_kw: gh_called.append(True) or (True, True),
        )

        hook_stdin = json.dumps(
            {
                "session_id": f"{short_id}-315-idea-uuid",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            }
        )
        runner = CliRunner()
        with freeze_time(self._BUDGET_EXCEEDED_HOOK_TIME):
            result = runner.invoke(main, ["signal-stop"], input=hook_stdin)
        assert result.exit_code == 0, result.output

        updated = next(s for s in load_state().sessions if s.id == session.id)
        assert updated.status == SessionStatus.TIMED_OUT
        assert not gh_called, "pr_is_merged_for_ticket must not be called for non-IMPL"


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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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

    def test_returns_true_when_sentinel_in_bash_tool_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel emitted via ``cat <<EOF`` lands in a tool_result block (#731).

        The worker prints the frame to stdout with Bash; it appears in a
        tool_result block (user-role record), not assistant text. signal_stop
        must still detect it, else last_result stays null and the stage stalls.
        """
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-731"
        worktree.mkdir(parents=True)
        frame = (
            "<<<AUTO_DEV_RESULT\n"
            '{"schema_version": 2, "ticket_id": "731", "status": "shipped"}\n'
            "AUTO_DEV_RESULT>>>\n"
        )
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Emitting the sentinel."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"cat <<'EOF'\n{frame}EOF"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": frame}],
                },
            },
        ]
        (project_dir / "uuid-toolresult.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

        assert _sentinel_present_in_transcript(str(worktree), "uuid-toolresult")

    def test_returns_false_when_no_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.cli import _sentinel_present_in_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
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
    # requires pr non-null but pr is null here. Using pr=null instead of the
    # former wait_for_ci violation because parse_stdout now coerces missing
    # wait_for_ci on shipped sentinels (issue #417). The pr=null invariant has
    # no coerce path, so it still produces BlockedResult(reason="validation_failed").
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
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "next_actions": ["wait_for_ci"]\n'
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

_SENTINEL_285_SHIPPED = (
    # Valid shipped sentinel for ticket "137" used in issue #285 regression tests.
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "shipped",\n'
    '  "stage_reached": "stage5_post_create",\n'
    '  "scope": {"tier": "small", "files": 2, "lines_estimate": 30, '
    '"lines_actual": 25, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": "auto-dev/137",\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": "abc285",\n'
    '  "commits": ["def285"],\n'
    '  "pr": {"number": 285, '
    '"url": "https://github.com/foo/bar/pull/285", '
    '"auto_merge": true, "base": "main"},\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, '
    '"fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", '
    '"any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", '
    '"downgrade_applied": false, "fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "next_actions": ["wait_for_ci"]\n'
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

# mirrors _premises_pending_payload in test_auto_dev_result.py at schema_version=2
_SENTINEL_316_PREMISES_PENDING_V2 = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "premises_pending_verification",\n'
    '  "stage_reached": "stage1_plan",\n'
    '  "scope": {"tier": "small", "files": 2, "lines_estimate": 40, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", "downgrade_applied": false, '
    '"fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "premises": [{"claim": "PR #198 codified a deliberate decision"}],\n'
    '  "ambiguities": [],\n'
    '  "next_actions": ["user_verify_premises"]\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

# mirrors _ambiguities_pending_payload in test_auto_dev_result.py at schema_version=2
_SENTINEL_316_AMBIGUITIES_PENDING_V2 = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 2,\n'
    '  "ticket_id": "137",\n'
    '  "status": "ambiguities_pending_resolution",\n'
    '  "stage_reached": "stage1_plan",\n'
    '  "scope": {"tier": "small", "files": 2, "lines_estimate": 40, '
    '"lines_actual": null, "forbidden_touched": false},\n'
    '  "plan_source": "linear_existing",\n'
    '  "branch": null,\n'
    '  "worktree_path": null,\n'
    '  "fork_point_sha": null,\n'
    '  "commits": [],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false, '
    '"shortcuts": [], "recommendation": "PROCEED", "downgrade_applied": false, '
    '"fix_loop_escalated": false},\n'
    '  "friction_highlights": [],\n'
    '  "blocker": null,\n'
    '  "ambiguities": [{"question": "Should we retry on timeout?"}],\n'
    '  "premises": [],\n'
    '  "next_actions": ["user_resolve_ambiguities"]\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)

# Sentinel for GitHub issue #263 regression tests.
# schema_version=99 is not in SUPPORTED_SCHEMA_VERSIONS, so parse_stdout
# returns BlockedResult(reason="schema_version_unsupported").
_SENTINEL_263_SCHEMA_VERSION_UNSUPPORTED = (
    "<<<AUTO_DEV_RESULT\n"
    '{"schema_version": 99, "status": "shipped"}\n'
    "AUTO_DEV_RESULT>>>"
)


class TestParseSentinelFromTranscript:
    """Tests for _parse_sentinel_from_transcript (GitHub issue #225).

    Headless DAEMON sessions complete via signal_stop and assign
    session.last_result, so their sentinels are captured before the
    orchestrator sees the SESSION_COMPLETED event. This helper walks
    the same Claude transcript JSONL the bool checker uses, but on a
    sentinel hit it returns the parsed AutoDevResult (or BlockedResult
    for malformed blocks) instead of throwing the parse away.
    """

    def _write_transcript(
        self,
        worktree: Path,
        claude_session_id: str,
        assistant_text: str,
        home: Path,
        extra_records: list[dict[str, object]] | None = None,
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
        prefix = ""
        if extra_records:
            prefix = "\n".join(json.dumps(r) for r in extra_records) + "\n"
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            prefix + json.dumps(record) + "\n"
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
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
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
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

    def test_last_match_skips_documented_example(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Documented example first, real sentinel last → real result returned.

        Regression for GitHub #591: the live-guard monitor latched onto the
        illustrative pr=42/PROJ-1234 block instead of the real result.
        """
        from cw.auto_dev_result import AutoDevResult
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-591a"
        worktree.mkdir(parents=True)

        example_payload = {
            "schema_version": 4,
            "ticket_id": "PROJ-1234",
            "status": "shipped",
            "stage_reached": "stage5_post_create",
            "scope": {
                "tier": "small",
                "files": 3,
                "lines_estimate": 42,
                "lines_actual": 47,
                "forbidden_touched": False,
            },
            "plan_source": "linear_existing",
            "branch": "dev/proj-1234-fix-login",
            "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
            "fork_point_sha": "abc1234",
            "commits": ["sha1", "sha2"],
            "pr": {
                "number": 42,
                "url": "https://github.com/.../pull/42",
                "auto_merge": True,
                "base": "main",
            },
            "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "MEDIUM",
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": None,
            "next_actions": ["wait_for_ci"],
        }
        example_frame = (
            f"<<<AUTO_DEV_RESULT\n{json.dumps(example_payload)}\nAUTO_DEV_RESULT>>>"
        )
        example_record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": example_frame}],
            },
        }
        self._write_transcript(
            worktree,
            "uuid-591a",
            _SENTINEL_215_PLAN_PENDING,
            fake_home,
            extra_records=[example_record],
        )

        parsed = _parse_sentinel_from_transcript(str(worktree), "uuid-591a")
        assert isinstance(parsed, AutoDevResult)
        assert parsed.ticket_id == "215"
        assert parsed.status == "plan_pending_approval"

    def test_example_only_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the documented example block present → None (no real sentinel).

        Regression for GitHub #591: a freshly-spawned session that only has
        the prompt's illustrative block must not report as shipped.
        """
        from cw.cli import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        worktree = tmp_path / "wt" / "auto-dev-591b"
        worktree.mkdir(parents=True)

        example_payload = {
            "schema_version": 4,
            "ticket_id": "PROJ-1234",
            "status": "shipped",
            "stage_reached": "stage5_post_create",
            "scope": {
                "tier": "small",
                "files": 3,
                "lines_estimate": 42,
                "lines_actual": 47,
                "forbidden_touched": False,
            },
            "plan_source": "linear_existing",
            "branch": "dev/proj-1234-fix-login",
            "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
            "fork_point_sha": "abc1234",
            "commits": ["sha1", "sha2"],
            "pr": {
                "number": 42,
                "url": "https://github.com/.../pull/42",
                "auto_merge": True,
                "base": "main",
            },
            "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "MEDIUM",
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": None,
            "next_actions": ["wait_for_ci"],
        }
        example_sentinel = (
            f"<<<AUTO_DEV_RESULT\n{json.dumps(example_payload)}\nAUTO_DEV_RESULT>>>"
        )
        self._write_transcript(worktree, "uuid-591b", example_sentinel, fake_home)

        assert _parse_sentinel_from_transcript(str(worktree), "uuid-591b") is None


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
        items = _complete_client(
            cast("click.Context", None), cast("click.Parameter", None), "a"
        )
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

        items = _complete_client(
            cast("click.Context", None), cast("click.Parameter", None), ""
        )
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

        items = _complete_session(
            cast("click.Context", None), cast("click.Parameter", None), ""
        )
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

        items = _complete_session(
            cast("click.Context", None), cast("click.Parameter", None), "alpha"
        )
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
        # Non-empty live set prevents reconcile's outage guard from
        # refusing to mutate state; "impl" ref still isn't live so phantom is reaped.
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
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
                    # started_at older than SPAWN_GRACE_SECONDS so reconcile
                    # treats this session as eligible for phantom-reaping
                    # (the grace window protects only freshly-spawned sessions).
                    started_at=datetime(2026, 4, 19, tzinfo=UTC),
                )
            ]
        )
        save_state(state)
        from cw.models import OrchestratorConfig, ReapPolicy

        monkeypatch.setattr(
            "cw.reconcile.core.load_orchestrator_config",
            lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
        )

        _display_status()

        output = capsys.readouterr().out
        # Real reconciler detects missing surface — session is reaped
        assert "Reaped phantom session: test-client/impl" in output
        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED


class TestBgNotifyCli:
    def test_bg_with_notify(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.background_session") as mock_bg:
            runner.invoke(main, ["bg", "--notify", "idea"])
            mock_bg.assert_called_once_with(None, notify="idea", auto=False)

    def test_bg_with_notify_short(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.sessions.background_session") as mock_bg:
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
            ["init", "my-repo", "--path", str(repo), "--no-onboarding"],
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
                "--no-onboarding",
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
            ["init", "--no-onboarding"],
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

    def test_init_no_onboarding(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """--no-onboarding skips all four onboarding functions."""
        repo = make_git_repo("my-repo")

        with (
            patch("cw.cli.maintenance.register_mcp_servers") as mock_mcp,
            patch("cw.cli.maintenance.install_cw_allowlist") as mock_allow,
            patch("cw.cli.maintenance.install_sessionstart_hook") as mock_hook,
            patch("cw.cli.maintenance.install_claude_md_snippet") as mock_md,
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["init", "my-repo", "--path", str(repo), "--no-onboarding"],
            )

        assert result.exit_code == 0, result.output
        assert "Added client 'my-repo'" in result.output
        mock_mcp.assert_not_called()
        mock_allow.assert_not_called()
        mock_hook.assert_not_called()
        mock_md.assert_not_called()

    def test_init_onboard_only(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """--onboard-only calls onboarding functions and skips init_client."""
        repo = make_git_repo("my-repo")

        # Pre-register the client so --onboard-only can find it.
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "my-repo", "--path", str(repo), "--no-onboarding"],
        )
        assert result.exit_code == 0, f"Setup failed: {result.output}"

        with (
            patch("cw.cli.maintenance.register_mcp_servers") as mock_mcp,
            patch("cw.cli.maintenance.install_cw_allowlist") as mock_allow,
            patch("cw.cli.maintenance.install_sessionstart_hook") as mock_hook,
            patch("cw.cli.maintenance.install_claude_md_snippet") as mock_md,
            patch("cw.cli.maintenance.init_client") as mock_init,
        ):
            result = runner.invoke(
                main,
                ["init", "my-repo", "--onboard-only"],
            )

        assert result.exit_code == 0, result.output
        assert "Onboarding complete" in result.output
        mock_init.assert_not_called()
        mock_mcp.assert_called_once()
        mock_allow.assert_called_once()
        mock_hook.assert_called_once()
        mock_md.assert_called_once()

    def test_init_onboard_only_missing_client(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """--onboard-only with nonexistent client name exits nonzero with error."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "no-such-client", "--onboard-only"],
        )

        assert result.exit_code != 0
        assert "no-such-client" in result.output

    def test_init_onboard_only_missing_name(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """--onboard-only without a name exits nonzero with error."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "--onboard-only"],
        )

        assert result.exit_code != 0
        assert "Name is required" in result.output

    def test_init_no_onboarding_and_onboard_only_conflict(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """--no-onboarding and --onboard-only together exit nonzero with error."""
        repo = make_git_repo("my-repo")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "init",
                "my-repo",
                "--path",
                str(repo),
                "--no-onboarding",
                "--onboard-only",
            ],
        )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_init_calls_onboarding_with_correct_workspace(
        self,
        tmp_config_dir: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default cw init calls all four onboarding functions with correct args."""
        from unittest.mock import MagicMock

        repo = make_git_repo("my-repo")

        mock_mcp = MagicMock()
        mock_allow = MagicMock()
        mock_hook = MagicMock()
        mock_md = MagicMock()

        monkeypatch.setattr("cw.cli.maintenance.register_mcp_servers", mock_mcp)
        monkeypatch.setattr("cw.cli.maintenance.install_cw_allowlist", mock_allow)
        monkeypatch.setattr("cw.cli.maintenance.install_sessionstart_hook", mock_hook)
        monkeypatch.setattr("cw.cli.maintenance.install_claude_md_snippet", mock_md)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["init", "my-repo", "--path", str(repo)],
        )

        assert result.exit_code == 0, result.output
        mock_mcp.assert_called_once_with(repo, "my-repo")
        mock_allow.assert_called_once()
        mock_hook.assert_called_once_with(repo)
        mock_md.assert_called_once_with(repo)


class TestQueueNextCli:
    def test_next_empty_queue(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.queues.peek_next", return_value=None):
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
        with patch("cw.cli.queues.peek_next", return_value=item):
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
        with patch("cw.cli.queues.peek_next", return_value=item):
            result = runner.invoke(
                main,
                ["queue", "next", "my-client", "--json"],
            )
            assert result.exit_code == 0
            assert '"description": "Fix bug"' in result.output

    def test_next_with_purpose(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.queues.peek_next", return_value=None) as mock_peek:
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
        with patch("cw.cli.queues.claim_next", return_value=None):
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
        with patch("cw.cli.queues.claim_next", return_value=item):
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
        with patch("cw.cli.queues.claim_next", return_value=item):
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
        with patch("cw.cli.queues.claim_by_id", return_value=item) as mock_claim:
            result = runner.invoke(
                main,
                ["queue", "claim", "my-client", "--id", "abc12345"],
            )
            assert result.exit_code == 0
            mock_claim.assert_called_once_with("my-client", "abc12345")

    def test_claim_with_purpose(self, tmp_config_dir: Path) -> None:
        runner = CliRunner()
        with patch("cw.cli.queues.claim_next", return_value=None) as mock_claim:
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
        with patch("cw.cli.queues.complete_item") as mock_complete:
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
        with patch("cw.cli.queues.complete_item") as mock_complete:
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
            "cw.cli.queues.complete_item",
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
        with patch("cw.cli.queues.fail_item") as mock_fail:
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
        with patch("cw.cli.queues.fail_item") as mock_fail:
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
            "cw.cli.queues.fail_item",
            side_effect=ValueError("Queue item not found: bad-id"),
        ):
            result = runner.invoke(
                main,
                ["queue", "fail", "my-client", "bad-id"],
            )
            assert result.exit_code != 0


class TestQueueListCli:
    def test_list_no_arg_requires_no_client(self, tmp_config_dir: Path) -> None:
        """No CLIENT arg should succeed (exit_code 0), not raise usage error."""
        runner = CliRunner()
        with patch("cw.cli.queues.load_clients", return_value={}):
            result = runner.invoke(main, ["queue", "list"])
            assert result.exit_code == 0
            assert "Queue is empty." in result.output

    def test_list_no_arg_shows_all_clients_with_items(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Cross-client view: shows clients with items, skips empty ones."""
        from cw.models import QueueStore

        alpha_item = QueueItem(
            client="alpha",
            task=TaskSpec(
                description="Task for alpha",
                purpose=SessionPurpose.IMPL,
                prompt="do it",
            ),
        )
        alpha_store = QueueStore(items=[alpha_item])
        beta_store = QueueStore(items=[])

        runner = CliRunner()
        clients = {
            "alpha": ClientConfig(
                name="alpha",
                workspace_path=tmp_path / "alpha",
            ),
            "beta": ClientConfig(
                name="beta",
                workspace_path=tmp_path / "beta",
            ),
        }
        with (
            patch("cw.cli.queues.load_clients", return_value=clients),
            patch(
                "cw.cli.queues.load_queue",
                side_effect=lambda c: alpha_store if c == "alpha" else beta_store,
            ),
        ):
            result = runner.invoke(main, ["queue", "list"])
            assert result.exit_code == 0
            assert "--- alpha ---" in result.output
            assert alpha_item.id in result.output
            assert "Task for alpha" in result.output
            assert "--- beta ---" not in result.output  # empty client skipped

    def test_list_no_arg_all_clients_empty(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """All clients empty → 'Queue is empty.'"""
        from cw.models import QueueStore

        runner = CliRunner()
        clients = {
            "alpha": ClientConfig(
                name="alpha",
                workspace_path=tmp_path / "alpha",
            ),
            "beta": ClientConfig(
                name="beta",
                workspace_path=tmp_path / "beta",
            ),
        }
        with (
            patch("cw.cli.queues.load_clients", return_value=clients),
            patch("cw.cli.queues.load_queue", return_value=QueueStore(items=[])),
        ):
            result = runner.invoke(main, ["queue", "list"])
            assert result.exit_code == 0
            assert "Queue is empty." in result.output

    def test_list_no_arg_zero_clients_empty(self, tmp_config_dir: Path) -> None:
        """Zero configured clients also shows 'Queue is empty.'"""
        runner = CliRunner()
        with patch("cw.cli.queues.load_clients", return_value={}):
            result = runner.invoke(main, ["queue", "list"])
            assert result.exit_code == 0
            assert "Queue is empty." in result.output

    def test_list_single_client_shows_items(self, tmp_config_dir: Path) -> None:
        """Existing per-client mode still works."""
        from cw.models import QueueStore

        item = QueueItem(
            client="my-client",
            task=TaskSpec(
                description="Fix bug",
                purpose=SessionPurpose.IMPL,
                prompt="fix it",
            ),
        )
        runner = CliRunner()
        with patch("cw.cli.queues.load_queue", return_value=QueueStore(items=[item])):
            result = runner.invoke(main, ["queue", "list", "my-client"])
            assert result.exit_code == 0
            assert item.id in result.output
            assert "Fix bug" in result.output

    def test_list_single_client_empty(self, tmp_config_dir: Path) -> None:
        """Per-client empty queue still says 'Queue is empty.'"""
        from cw.models import QueueStore

        runner = CliRunner()
        with patch("cw.cli.queues.load_queue", return_value=QueueStore(items=[])):
            result = runner.invoke(main, ["queue", "list", "my-client"])
            assert result.exit_code == 0
            assert "Queue is empty." in result.output

    def test_list_no_arg_purpose_filter(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--purpose filter applies in cross-client mode."""
        from cw.models import QueueStore

        impl_item = QueueItem(
            client="alpha",
            task=TaskSpec(
                description="impl task",
                purpose=SessionPurpose.IMPL,
                prompt="impl",
            ),
        )
        debt_item = QueueItem(
            client="alpha",
            task=TaskSpec(
                description="debt task",
                purpose=SessionPurpose.DEBT,
                prompt="debt",
            ),
        )
        store = QueueStore(items=[impl_item, debt_item])
        clients = {
            "alpha": ClientConfig(
                name="alpha",
                workspace_path=tmp_path / "alpha",
            ),
        }

        runner = CliRunner()
        with (
            patch("cw.cli.queues.load_clients", return_value=clients),
            patch("cw.cli.queues.load_queue", return_value=store),
        ):
            result = runner.invoke(main, ["queue", "list", "--purpose", "impl"])
            assert result.exit_code == 0
            assert "impl task" in result.output
            assert "debt task" not in result.output

    def test_list_no_arg_status_filter(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--status filter applies in cross-client mode."""
        from cw.models import QueueItemStatus, QueueStore

        pending_item = QueueItem(
            client="alpha",
            task=TaskSpec(
                description="pending task",
                purpose=SessionPurpose.IMPL,
                prompt="p",
            ),
        )
        running_item = QueueItem(
            client="alpha",
            task=TaskSpec(
                description="running task",
                purpose=SessionPurpose.IMPL,
                prompt="r",
            ),
        )
        running_item.status = QueueItemStatus.RUNNING
        store = QueueStore(items=[pending_item, running_item])
        clients = {
            "alpha": ClientConfig(
                name="alpha",
                workspace_path=tmp_path / "alpha",
            ),
        }

        runner = CliRunner()
        with (
            patch("cw.cli.queues.load_clients", return_value=clients),
            patch("cw.cli.queues.load_queue", return_value=store),
        ):
            result = runner.invoke(main, ["queue", "list", "--status", "running"])
            assert result.exit_code == 0
            assert "running task" in result.output
            assert "pending task" not in result.output


def test_display_status_reconciles_phantom_active_sessions(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cw status` reports and reaps sessions with missing surfaces."""
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
                    # Older than SPAWN_GRACE_SECONDS so the spawn-grace
                    # window doesn't protect this from phantom-reaping.
                    started_at=datetime(2026, 4, 19, tzinfo=UTC),
                ),
            ]
        )
    )

    # Non-empty live set prevents the outage guard from aborting reconcile;
    # the "gone" surface_ref still isn't in the set so phantom1 is reaped.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    from cw.models import OrchestratorConfig, ReapPolicy

    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "Reaped phantom session" in result.output
    assert "client-a/impl" in result.output

    reloaded = load_state()
    reaped = reloaded.find_by_name_or_id("phantom1")
    assert reaped is not None
    assert reaped.status == SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# TestDevQueueRefreshAll
# ---------------------------------------------------------------------------


class TestDevQueueRefreshAll:
    """Tests for `cw dev-queue refresh-all`."""

    def _write_clients_yaml(
        self,
        tmp_config_dir: Path,
        clients: list[tuple[str, str]],
    ) -> None:
        """Write a minimal clients.yaml with the given (name, workspace_path) tuples."""
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        lines = ["clients:\n"]
        for name, ws in clients:
            lines.append(f"  {name}:\n")
            lines.append(f"    workspace_path: {ws}\n")
        (config_dir / "clients.yaml").write_text("".join(lines))

    def test_refresh_all_runs_fast_forward_for_each_client(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fast_forward_main called once per configured client, exit 0."""
        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        ws_b = tmp_path / "ws-b"
        ws_b.mkdir()
        self._write_clients_yaml(
            tmp_config_dir,
            [("client-a", str(ws_a)), ("client-b", str(ws_b))],
        )

        called_clients: list[str] = []

        def _mock_ff(client: object, **_kwargs: object) -> tuple[str, str]:
            from cw.models import ClientConfig

            assert isinstance(client, ClientConfig)
            called_clients.append(client.name)
            return (
                "sha1sha1sha1sha1sha1sha1sha1sha1sha1sha1",
                "sha2sha2sha2sha2sha2sha2sha2sha2sha2sha2",
            )

        monkeypatch.setattr("cw.cli.dev_queue.fast_forward_main", _mock_ff)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 0, result.output
        assert set(called_clients) == {"client-a", "client-b"}

    def test_refresh_all_prints_already_up_to_date_when_same_sha(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same before/after SHA → 'already up to date' in output."""
        ws = tmp_path / "ws"
        ws.mkdir()
        self._write_clients_yaml(tmp_config_dir, [("my-client", str(ws))])

        monkeypatch.setattr(
            "cw.cli.dev_queue.fast_forward_main",
            lambda _c, **_kw: (
                "abc123def456abc123def456abc123def456abc1",
                "abc123def456abc123def456abc123def456abc1",
            ),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 0
        assert "already up to date" in result.output.lower()

    def test_refresh_all_prints_updated_sha_when_changed(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Different before/after SHA → output shows both SHAs."""
        ws = tmp_path / "ws"
        ws.mkdir()
        self._write_clients_yaml(tmp_config_dir, [("my-client", str(ws))])

        monkeypatch.setattr(
            "cw.cli.dev_queue.fast_forward_main",
            lambda _c, **_kw: (
                "oldsha1oldsha1oldsha1oldsha1oldsha1oldsh",
                "newsha2newsha2newsha2newsha2newsha2newsh",
            ),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 0
        assert "oldsha1" in result.output
        assert "newsha2" in result.output

    def test_refresh_all_continues_on_one_client_failure(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WorktreeError for one client → other client still called, exit non-zero."""
        from cw.exceptions import WorktreeError

        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        ws_b = tmp_path / "ws-b"
        ws_b.mkdir()
        self._write_clients_yaml(
            tmp_config_dir,
            [("client-a", str(ws_a)), ("client-b", str(ws_b))],
        )

        called_clients: list[str] = []

        def _mock_ff(client: object, **_kwargs: object) -> tuple[str, str]:
            from cw.models import ClientConfig

            assert isinstance(client, ClientConfig)
            called_clients.append(client.name)
            if client.name == "client-a":
                msg = "ff failed"
                raise WorktreeError(msg)
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.cli.dev_queue.fast_forward_main", _mock_ff)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 1
        assert "client-b" in called_clients
        assert "client-a" in called_clients

    def test_refresh_all_emits_no_events(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """refresh-all does NOT emit ticket.needs_sync events."""
        from cw.events import read_events as _read_events
        from cw.models import OrchestratorEventType

        ws = tmp_path / "ws"
        ws.mkdir()
        self._write_clients_yaml(tmp_config_dir, [("my-client", str(ws))])

        monkeypatch.setattr(
            "cw.cli.dev_queue.fast_forward_main",
            lambda _c, **_kw: ("aaa", "bbb"),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 0, result.output

        events = _read_events(
            consumer="test-refresh-no-events",
            event_types=[OrchestratorEventType.TICKET_NEEDS_SYNC],
        )
        assert len(events) == 0

    def test_refresh_all_skips_missing_workspace_client(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MissingWorkspaceError for one client -> SKIP line on stderr.

        Other clients should still be called.
        """
        from cw.exceptions import MissingWorkspaceError

        ws_b = tmp_path / "ws-b"
        ws_b.mkdir()
        self._write_clients_yaml(
            tmp_config_dir,
            [("client-a", str(tmp_path / "nonexistent")), ("client-b", str(ws_b))],
        )

        called_clients: list[str] = []

        def _mock_ff(client: object, **_kwargs: object) -> tuple[str, str]:
            from cw.models import ClientConfig

            assert isinstance(client, ClientConfig)
            called_clients.append(client.name)
            if client.name == "client-a":
                msg = "workspace missing for client-a"
                raise MissingWorkspaceError(msg)
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.cli.dev_queue.fast_forward_main", _mock_ff)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert "client-a" in called_clients
        assert "client-b" in called_clients
        assert "SKIP" in result.output

    def test_refresh_all_missing_workspace_does_not_set_exit_1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing workspace alone -> exit code 0."""
        from cw.exceptions import MissingWorkspaceError

        ws = tmp_path / "nonexistent"
        self._write_clients_yaml(tmp_config_dir, [("client-a", str(ws))])

        def _mock_ff(client: object, **_kwargs: object) -> tuple[str, str]:
            msg = "workspace missing for client-a"
            raise MissingWorkspaceError(msg)

        monkeypatch.setattr("cw.cli.dev_queue.fast_forward_main", _mock_ff)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 0

    def test_refresh_all_missing_workspace_mixed_with_real_failure(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One missing workspace (soft skip) + one real WorktreeError.

        Expects exit 1 and both SKIP and ERROR messages printed.
        """
        from cw.exceptions import MissingWorkspaceError, WorktreeError

        ws_c = tmp_path / "ws-c"
        ws_c.mkdir()
        self._write_clients_yaml(
            tmp_config_dir,
            [
                ("client-a", str(tmp_path / "nonexistent")),
                ("client-b", str(tmp_path / "ws-b")),
                ("client-c", str(ws_c)),
            ],
        )

        def _mock_ff(client: object, **_kwargs: object) -> tuple[str, str]:
            from cw.models import ClientConfig

            assert isinstance(client, ClientConfig)
            if client.name == "client-a":
                msg = "workspace missing for client-a"
                raise MissingWorkspaceError(msg)
            if client.name == "client-b":
                msg = "ff failed"
                raise WorktreeError(msg)
            return ("aaa", "bbb")

        monkeypatch.setattr("cw.cli.dev_queue.fast_forward_main", _mock_ff)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "refresh-all"])
        assert result.exit_code == 1
        assert "SKIP" in result.output
        assert "ERROR" in result.output


# ---------------------------------------------------------------------------
# TestDevQueueAddTimeout (GitHub issue #265)
# ---------------------------------------------------------------------------


class TestDevQueueAddTimeout:
    """Tests for ``--timeout`` flag on ``cw dev-queue add``."""

    def test_dev_queue_add_timeout_flag(self, tmp_config_dir: Path) -> None:
        """--timeout sets headless_timeout_override on the created TicketTask."""
        from cw.dev_queue import load_dev_queue

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "add",
                "GEN-123",
                "--client",
                "client-a",
                "--timeout",
                "5400",
            ],
        )
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next((t for t in store.tasks if t.ticket_id == "GEN-123"), None)
        assert task is not None
        assert task.headless_timeout_override == 5400

    def test_dev_queue_add_no_timeout(self, tmp_config_dir: Path) -> None:
        """Without --timeout, headless_timeout_override is None."""
        from cw.dev_queue import load_dev_queue

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "add", "GEN-456", "--client", "client-b"],
        )
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next((t for t in store.tasks if t.ticket_id == "GEN-456"), None)
        assert task is not None
        assert task.headless_timeout_override is None


# ---------------------------------------------------------------------------
# TestOrchestratorStart (GitHub issue #295)
# ---------------------------------------------------------------------------


def _write_clients_yaml_for_test(
    tmp_config_dir: Path,
    clients: list[tuple[str, str]],
) -> None:
    """Write a minimal clients.yaml with the given (name, workspace_path) tuples."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["clients:\n"]
    for name, ws in clients:
        lines.append(f"  {name}:\n")
        lines.append(f"    workspace_path: {ws}\n")
    (config_dir / "clients.yaml").write_text("".join(lines))


def _write_staged_clients_yaml_for_test(
    tmp_config_dir: Path,
    client_name: str,
    workspace_path: str = "/tmp/ws-test",
) -> None:
    """Write a staged clients.yaml for B2 advance decision tests.

    Required when a sentinel routes through apply_staged_decision, which
    calls _stage_advance and needs the client's pipeline on disk (#698).
    """
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n"
        f"  {client_name}:\n"
        f"    workspace_path: {workspace_path}\n"
        f"    default_branch: main\n"
        f"    pipeline:\n"
        f"      stages: [plan, impl, review, finalize]\n"
    )


def _make_git_workspace_for_test(tmp_path: Path, name: str) -> Path:
    """Create a minimal git repo suitable for spawn_create_impl's _validate_worktree."""
    import os
    import subprocess

    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=True,
            env=clean_env,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "t@t.com")
    _git("config", "user.name", "t")
    _git("commit", "--allow-empty", "-m", "init")
    return repo


class TestOrchestratorStart:
    """Tests for ``cw orchestrator-start`` command."""

    def test_orchestrator_start_with_explicit_client_spawns_session(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cw orchestrator-start --client mytest spawns a session and prints its id."""
        from cw.native_daemon import FakeNativeDaemonClient

        ws = _make_git_workspace_for_test(tmp_path, "ws-explicit")
        _write_clients_yaml_for_test(tmp_config_dir, [("mytest", str(ws))])
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrator-start", "--client", "mytest"])

        assert result.exit_code == 0, result.output
        assert len(daemon.spawn_calls) == 1

    def test_orchestrator_start_no_clients_raises_clickexception(
        self, tmp_config_dir: Path
    ) -> None:
        """When no clients are configured, command exits with error."""
        runner = CliRunner()
        result = runner.invoke(main, ["orchestrator-start"])

        assert result.exit_code != 0
        assert "No clients configured" in result.output

    def test_orchestrator_start_unknown_client_raises_clickexception(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--client unknown-name exits with error mentioning the unknown name."""
        ws = tmp_path / "ws-unknown"
        ws.mkdir()
        _write_clients_yaml_for_test(tmp_config_dir, [("real-client", str(ws))])

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrator-start", "--client", "unknown-name"])

        assert result.exit_code != 0
        assert "unknown-name" in result.output

    def test_orchestrator_start_defaults_to_first_client_when_omitted(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --client, command uses the first configured client."""
        from cw.native_daemon import FakeNativeDaemonClient

        ws = _make_git_workspace_for_test(tmp_path, "ws-default")
        _write_clients_yaml_for_test(tmp_config_dir, [("first-client", str(ws))])
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrator-start"])

        assert result.exit_code == 0, result.output
        assert len(daemon.spawn_calls) == 1
        assert daemon.spawn_calls[0][0] == ws

    def test_orchestrator_start_passes_correct_extra_args_to_spawn_create_impl(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """extra_args contain --agent and --dangerously-load-development-channels."""
        from cw.cli import _ORCHESTRATOR_AGENT, _ORCHESTRATOR_CHANNEL
        from cw.native_daemon import FakeNativeDaemonClient

        ws = _make_git_workspace_for_test(tmp_path, "ws-args")
        _write_clients_yaml_for_test(tmp_config_dir, [("args-client", str(ws))])
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: daemon)

        runner = CliRunner()
        result = runner.invoke(main, ["orchestrator-start", "--client", "args-client"])

        assert result.exit_code == 0, result.output
        assert daemon.spawn_extra_args[0] is not None
        extra = daemon.spawn_extra_args[0]
        assert "--agent" in extra
        assert _ORCHESTRATOR_AGENT in extra
        assert "--dangerously-load-development-channels" in extra
        assert _ORCHESTRATOR_CHANNEL in extra
        assert daemon.spawn_permission_modes[0] == "acceptEdits"


# ---------------------------------------------------------------------------
# TestSpawnCloseTaskCancellation — issue #317
# ---------------------------------------------------------------------------


class TestSpawnCloseTaskCancellation:
    """_spawn_close_impl must atomically CANCEL the owning RUNNING TicketTask."""

    def _make_daemon_session(
        self, tmp_path: Path, sess_id: str = "close-sess-1"
    ) -> Session:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        sess = Session(
            id=sess_id,
            name=f"test-client/auto-dev/{sess_id}",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=workspace,
            status=SessionStatus.ACTIVE,
            surface_ref="fake-ref",
        )
        state = load_state()
        state.sessions.append(sess)
        save_state(state)
        return sess

    def test_spawn_close_cancels_running_task(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON session + RUNNING task with session_id → after close → CANCELLED."""
        from cw.cli import _spawn_close_impl
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        sess = self._make_daemon_session(tmp_path)
        task = TicketTask(
            ticket_id="CLOSE-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "CLOSE-1")
        assert t.status == QueueItemStatus.CANCELLED
        assert t.session_id is None

    def test_spawn_close_user_origin_does_not_touch_task(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """USER-origin session close → RUNNING task is NOT cancelled."""
        from cw.cli import _spawn_close_impl
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        workspace = tmp_path / "workspace2"
        workspace.mkdir(parents=True, exist_ok=True)
        sess = Session(
            id="user-close-sess",
            name="test-client/auto-dev/user-close-sess",
            client="test-client",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.USER,
            workspace_path=workspace,
            status=SessionStatus.ACTIVE,
            surface_ref=None,
        )
        state = load_state()
        state.sessions.append(sess)
        save_state(state)

        task = TicketTask(
            ticket_id="USER-CLOSE-1",
            client="test-client",
            status=QueueItemStatus.RUNNING,
            session_id=sess.id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        daemon = FakeNativeDaemonClient()
        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "USER-CLOSE-1")
        assert t.status == QueueItemStatus.RUNNING

    def test_spawn_close_no_task_for_session_is_ok(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """DAEMON session with no matching task → close succeeds without error."""
        from cw.cli import _spawn_close_impl
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore
        from cw.native_daemon import FakeNativeDaemonClient

        sess = self._make_daemon_session(tmp_path, sess_id="no-task-sess")
        save_dev_queue(DevQueueStore(tasks=[]))

        daemon = FakeNativeDaemonClient()
        # Should not raise.
        _spawn_close_impl(session_id=sess.id, native_daemon=daemon)

        state = load_state()
        updated = next(s for s in state.sessions if s.id == sess.id)
        assert updated.status == SessionStatus.COMPLETED


class TestPeek:
    """Tests for `cw peek` — read-only session output snapshot from transcript."""

    def _make_session(
        self,
        tmp_path: Path,
        *,
        status: SessionStatus = SessionStatus.ACTIVE,
        claude_session_id: str | None = "abc12345",
        sess_id: str = "peeksess",
        name: str = "test-client/impl",
    ) -> Session:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        session = Session(
            id=sess_id,
            name=name,
            client="test-client",
            purpose=SessionPurpose.IMPL,
            status=status,
            workspace_path=workspace,
            worktree_path=worktree,
            claude_session_id=claude_session_id,
        )
        state = load_state()
        state.sessions.append(session)
        save_state(state)
        return session

    def _write_transcript(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        session: Session,
        text: str,
    ) -> None:
        """Place a Claude-shaped transcript holding one assistant text block.

        Patches ``Path.home`` so ``claude_project_dir`` resolves under the
        tmp tree rather than the real ``~/.claude/projects``.
        """
        fake_home = tmp_path / "fake-home"
        cwd = session.worktree_path or session.workspace_path
        encoded = str(cwd).replace("/", "-").replace(".", "-")
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
        (transcript_dir / f"{session.claude_session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

    @staticmethod
    def _patch_empty_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Point ``Path.home`` at an empty tmp home (no transcript present)."""
        monkeypatch.setattr(
            "cw.cli.sessions.Path.home", lambda: tmp_path / "empty-home"
        )

    def test_peek_happy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._make_session(tmp_path)
        self._write_transcript(monkeypatch, tmp_path, session, "hello world")

        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 0, result.output
        assert "hello world" in result.output

    def test_peek_by_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._make_session(tmp_path, sess_id="id99999")
        self._write_transcript(monkeypatch, tmp_path, session, "output via id")

        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.id])
        assert result.exit_code == 0, result.output
        assert "output via id" in result.output

    def test_peek_unknown_session_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["peek", "no-such-session"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_peek_completed_session_exits_1(self, tmp_path: Path) -> None:
        session = self._make_session(tmp_path, status=SessionStatus.COMPLETED)
        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 1
        assert "completed" in result.output

    def test_peek_no_claude_session_id_exits_1(self, tmp_path: Path) -> None:
        session = self._make_session(tmp_path, claude_session_id=None)
        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 1
        assert "Claude session id" in result.output

    def test_peek_missing_transcript_exits_1_with_suggestion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Session is ACTIVE with a claude_session_id, but no transcript exists.
        session = self._make_session(tmp_path)
        self._patch_empty_home(monkeypatch, tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 1
        assert "post-mortem" in result.output

    def test_peek_default_lines_tails_50(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._make_session(tmp_path)
        body = "\n".join(f"line{i}" for i in range(60))
        self._write_transcript(monkeypatch, tmp_path, session, body)

        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 0, result.output
        emitted = result.output.strip().splitlines()
        assert len(emitted) == 50
        assert emitted[-1] == "line59"
        assert emitted[0] == "line10"

    def test_peek_custom_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._make_session(tmp_path)
        body = "\n".join(f"line{i}" for i in range(60))
        self._write_transcript(monkeypatch, tmp_path, session, body)

        runner = CliRunner()
        result = runner.invoke(main, ["peek", "--lines", "10", session.name])
        assert result.exit_code == 0, result.output
        emitted = result.output.strip().splitlines()
        assert len(emitted) == 10
        assert emitted[-1] == "line59"

    def test_peek_scrollback_bounds_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # scrollback caps how many trailing transcript lines are considered:
        # with 60 lines but --scrollback 5, only the last 5 are eligible, so
        # --lines 50 yields 5. No "fewer than" warning because the user
        # intentionally capped the window via --scrollback.
        session = self._make_session(tmp_path)
        body = "\n".join(f"line{i}" for i in range(60))
        self._write_transcript(monkeypatch, tmp_path, session, body)

        runner = CliRunner()
        result = runner.invoke(
            main, ["peek", "--scrollback", "5", "--lines", "50", session.name]
        )
        assert result.exit_code == 0, result.output
        emitted = [ln for ln in result.output.splitlines() if ln.startswith("line")]
        assert emitted == ["line55", "line56", "line57", "line58", "line59"]
        assert "fewer than" not in result.stderr

    def test_peek_warns_when_fewer_lines_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = self._make_session(tmp_path)
        self._write_transcript(monkeypatch, tmp_path, session, "line1\nline2\nline3")

        runner = CliRunner()
        result = runner.invoke(main, ["peek", "--lines", "50", session.name])
        assert result.exit_code == 0
        assert "fewer than" in result.stderr
        # The available content is still emitted alongside the warning.
        assert "line1" in result.output
        assert "line3" in result.output

    def test_peek_transcript_with_no_assistant_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transcript holding only non-assistant records → exit 0, no output.

        No assistant text means nothing to surface; peek succeeds quietly
        rather than erroring, and emits no warning (empty content).
        """
        session = self._make_session(tmp_path)
        fake_home = tmp_path / "fake-home"
        cwd = session.worktree_path or session.workspace_path
        encoded = str(cwd).replace("/", "-").replace(".", "-")
        transcript_dir = fake_home / ".claude" / "projects" / encoded
        transcript_dir.mkdir(parents=True, exist_ok=True)
        user_record = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
        (transcript_dir / f"{session.claude_session_id}.jsonl").write_text(
            json.dumps(user_record) + "\n"
        )
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)

        runner = CliRunner()
        result = runner.invoke(main, ["peek", session.name])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == ""


class TestWatchCommand:
    def test_watch_help(self) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["watch", "--help"])
        assert result.exit_code == 0
        assert "--interval" in result.output

    def test_watch_invokes_watch_flat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from cw.cli import main
        from cw.cli import sessions as cli

        called: list[object] = []
        monkeypatch.setattr(cli, "watch_flat", lambda **kwargs: called.append(kwargs))
        runner = CliRunner()
        result = runner.invoke(main, ["watch"])
        assert result.exit_code == 0
        assert called


# ---------------------------------------------------------------------------
# TestDevQueueRunQuiet
# ---------------------------------------------------------------------------


class TestDevQueueRunQuiet:
    """--quiet flag for cw dev-queue run suppresses operator stdout."""

    def test_quiet_flag_suppresses_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With --quiet, run_dispatch_loop is called with emit=None."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        captured_emit: list[object] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_emit.append(emit)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "run", "--once", "--quiet"])
        assert result.exit_code == 0, result.output
        assert captured_emit == [None], (
            f"Expected emit=None for --quiet but got: {captured_emit!r}"
        )

    def test_verbose_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without --quiet, run_dispatch_loop is called with a non-None emit."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        captured_emit: list[object] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_emit.append(emit)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "run", "--once"])
        assert result.exit_code == 0, result.output
        assert len(captured_emit) == 1
        assert callable(captured_emit[0]), (
            f"Expected callable emit but got: {captured_emit[0]!r}"
        )

    def test_auto_ff_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bare invocation passes auto_ff=True to run_dispatch_loop."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        captured_auto_ff: list[bool] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_auto_ff.append(auto_ff)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "run", "--once"])
        assert result.exit_code == 0, result.output
        assert captured_auto_ff == [True], (
            f"Expected auto_ff=True by default but got: {captured_auto_ff!r}"
        )

    def test_no_auto_ff_flag_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--no-auto-ff passes auto_ff=False to run_dispatch_loop."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        captured_auto_ff: list[bool] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_auto_ff.append(auto_ff)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "run", "--once", "--no-auto-ff"])
        assert result.exit_code == 0, result.output
        assert captured_auto_ff == [False], (
            f"Expected auto_ff=False with --no-auto-ff but got: {captured_auto_ff!r}"
        )


# ---------------------------------------------------------------------------
# Tests: dev-queue run --client
# ---------------------------------------------------------------------------


class TestDevQueueRunClientFilter:
    """Tests for --client/-c scoping on cw dev-queue run."""

    def test_client_flag_passed_to_run_dispatch_loop(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--client X passes client='X' to run_dispatch_loop."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        _write_clients_yaml_for_test(tmp_config_dir, [("my-client", str(tmp_path))])

        captured_client: list[str | None] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_client.append(client)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "run", "--once", "--client", "my-client"]
        )
        assert result.exit_code == 0, result.output
        assert captured_client == ["my-client"], (
            f"Expected client='my-client' but got: {captured_client!r}"
        )

    def test_no_client_flag_passes_none_to_run_dispatch_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting --client passes client=None to run_dispatch_loop."""
        from cw.cli import dev_queue as cli_module
        from cw.cli import main

        captured_client: list[str | None] = []

        def _fake_loop(
            *,
            max_parallel: object = None,
            once: bool = False,
            use_plan: bool = False,
            parent: object = None,
            native_daemon: object = None,
            emit: object = None,
            auto_ff: bool = True,
            client: str | None = None,
        ) -> None:
            captured_client.append(client)

        monkeypatch.setattr(cli_module, "run_dispatch_loop", _fake_loop)

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "run", "--once"])
        assert result.exit_code == 0, result.output
        assert captured_client == [None], (
            f"Expected client=None but got: {captured_client!r}"
        )

    def test_unknown_client_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--client unknown-name exits with non-zero and mentions the name."""
        from cw.cli import main

        _write_clients_yaml_for_test(tmp_config_dir, [("real-client", str(tmp_path))])

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "run", "--once", "--client", "unknown-name"]
        )
        assert result.exit_code != 0
        assert "unknown-name" in result.output


# ---------------------------------------------------------------------------
# Tests: _format_status_human
# ---------------------------------------------------------------------------


class TestFormatStatusHuman:
    def test_format_status_human_shows_last_tick_section(self) -> None:
        """_format_status_human renders last-tick section when data present."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus, TickSummary

        tick = TickSummary(
            claimed=2,
            pending=1,
            running=2,
            cap=3,
            skip_reason="none",
            tick_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            last_tick_by_client={"my-client": tick},
        )
        output = _format_status_human(status)
        assert "Last dispatch tick" in output
        assert "my-client" in output
        assert "claimed=2" in output
        assert "skip=none" in output

    def test_format_status_human_no_last_tick_when_empty(self) -> None:
        """_format_status_human shows 'no dispatch ticks' when empty."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus

        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
        )
        output = _format_status_human(status)
        assert "no dispatch ticks recorded" in output

    def test_format_status_human_last_stage_placeholder(
        self, tmp_config_dir: Path
    ) -> None:
        """Sessions with no stage events show placeholder text."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus, SessionSummary

        sess = SessionSummary(
            id="s1",
            name="test/impl/s1",
            client="test",
            status="active",
            purpose="impl",
            started_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            last_stage=None,
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            running_sessions=[sess],
        )
        output = _format_status_human(status)
        assert "(unknown — global auto-dev.md not yet emitting stage events)" in output

    def test_format_status_human_monitored_pr_shows_role(
        self, tmp_config_dir: Path
    ) -> None:
        """MonitoredPR line in _format_status_human includes role field."""
        from cw.cli import _format_status_human
        from cw.orchestrate import MonitoredPR, OrchestratorStatus

        pr = MonitoredPR(
            repo="owner/repo",
            pr_number=42,
            role="author",
            status="watching",
            unresolved_threads=0,
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            monitored_prs=[pr],
        )
        output = _format_status_human(status)
        assert "role=author" in output

    def test_format_status_human_pr_renders_ci_and_mergeable_populated(
        self, tmp_config_dir: Path
    ) -> None:
        """_format_status_human includes ci_status and mergeable when populated."""
        from cw.cli import _format_status_human
        from cw.orchestrate import MonitoredPR, OrchestratorStatus

        pr = MonitoredPR(
            repo="owner/repo",
            pr_number=7,
            role="author",
            status="watching",
            unresolved_threads=0,
            ci_status="success",
            mergeable=True,
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            monitored_prs=[pr],
        )
        output = _format_status_human(status)
        assert "ci=success" in output
        assert "mergeable=True" in output

    def test_format_status_human_pr_ci_none_renders_as_placeholder(
        self, tmp_config_dir: Path
    ) -> None:
        """_format_status_human renders (none) for None ci_status and mergeable."""
        from cw.cli import _format_status_human
        from cw.orchestrate import MonitoredPR, OrchestratorStatus

        pr = MonitoredPR(
            repo="owner/repo",
            pr_number=8,
            role="author",
            status="watching",
            unresolved_threads=0,
            # ci_status and mergeable default to None
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
            monitored_prs=[pr],
        )
        output = _format_status_human(status)
        assert "ci=(none)" in output
        assert "mergeable=(none)" in output

    def test_format_status_human_multi_lane_shows_indented_lines(self) -> None:
        """Multi-lane tick renders indented lane lines after the client summary."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus, TickSummary

        tick = TickSummary(
            claimed=1,
            pending=2,
            running=1,
            cap=3,
            skip_reason="none",
            tick_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            lanes={
                "fast": {"claimed": 1, "running": 1, "pending": 0},
                "slow": {"claimed": 0, "running": 0, "pending": 2},
            },
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            last_tick_by_client={"lane-client": tick},
        )
        output = _format_status_human(status)
        assert "    fast: claimed=1 running=1 blocked=0 pending=0" in output
        assert "    slow: claimed=0 running=0 blocked=0 pending=2" in output

    def test_format_status_human_single_default_lane_no_indented_lines(self) -> None:
        """Single 'default' lane tick does not render indented lane lines."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus, TickSummary

        tick = TickSummary(
            claimed=1,
            pending=0,
            running=1,
            cap=2,
            skip_reason="none",
            tick_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            lanes={"default": {"claimed": 1, "running": 1, "pending": 0}},
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            last_tick_by_client={"single-client": tick},
        )
        output = _format_status_human(status)
        # No indented lane breakdown when the only lane is DEFAULT_LANE
        assert "    default:" not in output
        assert "    fast:" not in output

    def test_format_status_human_empty_lanes_no_indented_lines(self) -> None:
        """Empty lanes dict renders no indented lane lines (legacy events)."""
        from cw.cli import _format_status_human
        from cw.orchestrate import OrchestratorStatus, TickSummary

        tick = TickSummary(
            claimed=0,
            pending=1,
            running=0,
            cap=2,
            skip_reason="none",
            tick_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            lanes={},
        )
        status = OrchestratorStatus(
            generated_at=datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC),
            last_tick_by_client={"empty-lanes-client": tick},
        )
        output = _format_status_human(status)
        # No lane breakdown lines at all
        assert "    " not in output.split("Last dispatch tick")[1].split("\n\n")[0]


class TestDevQueueStatusWithTick:
    def test_dev_queue_status_shows_last_tick(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dev-queue status renders last-tick section when tick data present."""
        from cw.dev_queue import add_ticket
        from cw.events import record_event
        from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

        add_ticket(
            TicketTask(
                ticket_id="GEN-999",
                client="tick-client",
                priority=5,
                status=QueueItemStatus.PENDING,
            )
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "tick-client",
                "claimed": 1,
                "pending": 0,
                "running": 1,
                "cap": 2,
                "skip_reason": "cap_full",
            },
        )

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "Last dispatch tick per client:" in result.output
        assert "tick-client" in result.output
        assert "claimed=1" in result.output
        assert "skip=cap_full" in result.output

    def test_dev_queue_status_multi_lane_shows_indented_lines(
        self, tmp_config_dir: Path
    ) -> None:
        """Multi-lane tasks produce indented lane breakdown after client summary."""
        from cw.dev_queue import add_ticket
        from cw.events import record_event
        from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

        add_ticket(
            TicketTask(
                ticket_id="GEN-1",
                client="multi-client",
                priority=5,
                status=QueueItemStatus.PENDING,
                lane="fast",
            )
        )
        add_ticket(
            TicketTask(
                ticket_id="GEN-2",
                client="multi-client",
                priority=3,
                status=QueueItemStatus.PENDING,
                lane="slow",
            )
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "multi-client",
                "claimed": 0,
                "pending": 2,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                "lanes": {
                    "fast": {"claimed": 0, "running": 0, "pending": 1},
                    "slow": {"claimed": 0, "running": 0, "pending": 1},
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "    lane fast:" in result.output
        assert "    lane slow:" in result.output

    def test_dev_queue_status_single_default_lane_no_indented_lines(
        self, tmp_config_dir: Path
    ) -> None:
        """Single default-lane tick renders no indented lane lines."""
        from cw.dev_queue import add_ticket
        from cw.events import record_event
        from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

        add_ticket(
            TicketTask(
                ticket_id="GEN-3",
                client="default-client",
                priority=5,
                status=QueueItemStatus.PENDING,
            )
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "default-client",
                "claimed": 0,
                "pending": 1,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                "lanes": {"default": {"claimed": 0, "running": 0, "pending": 1}},
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        # No indented lane breakdown for single default lane
        assert "    lane default:" not in result.output
        assert "    lane fast:" not in result.output

    def test_dev_queue_status_blocked_on_user_shows_in_lane(
        self, tmp_config_dir: Path
    ) -> None:
        """BLOCKED_ON_USER task in non-default lane shows blocked=1 in lane line."""
        from cw.dev_queue import add_ticket
        from cw.events import record_event
        from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

        add_ticket(
            TicketTask(
                ticket_id="GEN-4",
                client="blocked-client",
                priority=5,
                status=QueueItemStatus.BLOCKED_ON_USER,
                lane="fast",
            )
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": "blocked-client",
                "claimed": 0,
                "pending": 0,
                "running": 0,
                "cap": 2,
                "skip_reason": "none",
                "lanes": {"fast": {"claimed": 0, "running": 0, "pending": 0}},
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "    lane fast:" in result.output
        assert "blocked=1" in result.output

    def test_dev_queue_status_blocked_column_in_main_table(
        self, tmp_config_dir: Path
    ) -> None:
        """BLOCKED_ON_USER task appears in main table BLOCKED column. See #633."""
        from cw.dev_queue import add_ticket
        from cw.models import QueueItemStatus, TicketTask

        add_ticket(
            TicketTask(
                ticket_id="GEN-633",
                client="approval-client",
                priority=5,
                status=QueueItemStatus.BLOCKED_ON_USER,
            )
        )
        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "status"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "approval-client" in result.output
        # BLOCKED column (7-wide, right-aligned) must show 1; all others must be 0.
        for line in result.output.splitlines():
            if "approval-client" in line:
                parts = line.split()
                # parts: [client, pending, running, blocked, completed, cancelled, ...]
                assert parts[3] == "1", f"BLOCKED count wrong: {line!r}"
                assert parts[1] == "0", f"PENDING should be 0: {line!r}"
                assert parts[4] == "0", f"COMPLETED should be 0: {line!r}"
                break


# ---------------------------------------------------------------------------
# TestDevQueueWait (GitHub issue #474)
# ---------------------------------------------------------------------------


class TestDevQueueWait:
    """Tests for ``cw dev-queue wait``."""

    def _seed_task(
        self,
        tmp_config_dir: Path,
        ticket_id: str,
        status: QueueItemStatus,
        session_id: str | None = "sess-wait",
    ) -> None:
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, TicketTask

        store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="genhealth",
                    status=status,
                    session_id=session_id,
                )
            ]
        )
        save_dev_queue(store)

    def test_wait_completed_exit_0(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COMPLETED ticket → exit 0."""
        from cw.cli import _WAIT_EXIT_FAILED
        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-10",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            session_id="sess-10",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-10", "--client", "genhealth"]
        )
        assert result.exit_code == 0, result.output
        assert _WAIT_EXIT_FAILED != 0  # sanity: 0 is distinct from FAILED

    def test_wait_failed_exit_1(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILED ticket → exit _WAIT_EXIT_FAILED (1)."""
        from cw.cli import _WAIT_EXIT_FAILED
        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-11",
            client="genhealth",
            status=QueueItemStatus.FAILED,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-11", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_FAILED

    def test_wait_cancelled_exit_1(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CANCELLED ticket → exit _WAIT_EXIT_FAILED (1)."""
        from cw.cli import _WAIT_EXIT_FAILED
        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-12",
            client="genhealth",
            status=QueueItemStatus.CANCELLED,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-12", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_FAILED

    def test_wait_blocked_on_user_exit_2(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BLOCKED_ON_USER with no reap_proposed_at → exit _WAIT_EXIT_BLOCKED (2)."""
        from cw.cli import _WAIT_EXIT_BLOCKED
        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-13",
            client="genhealth",
            status=QueueItemStatus.BLOCKED_ON_USER,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-13", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_BLOCKED

    def test_wait_blocked_on_user_reap_proposed_exit_attention(
        self, tmp_config_dir: Path
    ) -> None:
        """BLOCKED_ON_USER with reap_proposed_at set → ATTENTION (exit 3, #542 fix)."""
        import pathlib

        from cw.cli import _WAIT_EXIT_ATTENTION
        from cw.dev_queue import save_dev_queue
        from cw.models import (
            DevQueueStore,
            QueueItemStatus,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
            TicketTask,
        )

        sess = Session(
            id="reap-sess-42",
            name="genhealth/auto-dev/GEN-542",
            client="genhealth",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            origin=SessionOrigin.DAEMON,
            workspace_path=pathlib.Path("/tmp/ws"),
            reap_proposed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        save_state(CwState(sessions=[sess]))

        store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GEN-542",
                    client="genhealth",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id="reap-sess-42",
                )
            ]
        )
        save_dev_queue(store)

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-542", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_ATTENTION

    def test_wait_timeout_exit_124(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TimeoutError from wait_for_terminal → exit _WAIT_EXIT_TIMEOUT (124)."""
        from cw.cli import _WAIT_EXIT_TIMEOUT

        def _raise_timeout(ticket_id: str, client: str, *, timeout: float) -> None:
            raise TimeoutError

        monkeypatch.setattr("cw.cli.dev_queue.wait_for_terminal", _raise_timeout)
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-14", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_TIMEOUT

    def test_wait_json_output_shape(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--json emits all 4 required keys with correct values."""
        import json as _json

        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-15",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            session_id="sess-15",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-15", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip())
        assert payload["ticket_id"] == "GEN-15"
        assert payload["client"] == "genhealth"
        assert payload["status"] == "completed"
        assert payload["session_id"] == "sess-15"

    def test_wait_json_session_id_null(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--json emits session_id: null (not omitted) when session_id is None."""
        import json as _json

        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-16",
            client="genhealth",
            status=QueueItemStatus.FAILED,
            session_id=None,
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-16", "--client", "genhealth", "--json"],
        )
        from cw.cli import _WAIT_EXIT_FAILED

        assert result.exit_code == _WAIT_EXIT_FAILED
        payload = _json.loads(result.output.strip())
        assert "session_id" in payload
        assert payload["session_id"] is None

    def test_wait_json_timeout(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--json on timeout emits status=timeout with session_id: null."""
        import json as _json

        from cw.cli import _WAIT_EXIT_TIMEOUT

        def _raise_timeout(ticket_id: str, client: str, *, timeout: float) -> None:
            raise TimeoutError

        monkeypatch.setattr("cw.cli.dev_queue.wait_for_terminal", _raise_timeout)
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "wait",
                "GEN-17",
                "--client",
                "genhealth",
                "--json",
            ],
        )
        assert result.exit_code == _WAIT_EXIT_TIMEOUT
        payload = _json.loads(result.output.strip())
        assert payload["status"] == "timeout"
        assert payload["session_id"] is None

    def test_wait_timeout_option_wiring(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--timeout value is forwarded to wait_for_terminal."""
        from cw.models import QueueItemStatus, TicketTask

        captured: list[float] = []
        task = TicketTask(
            ticket_id="GEN-18",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
        )

        def _capture(ticket_id: str, client: str, *, timeout: float) -> TicketTask:
            captured.append(timeout)
            return task

        monkeypatch.setattr("cw.cli.dev_queue.wait_for_terminal", _capture)
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "wait",
                "GEN-18",
                "--client",
                "genhealth",
                "--timeout",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output
        assert captured == [42.0]

    def test_wait_exit_codes_match_constants(self) -> None:
        """Named exit-code constants have the expected integer values."""
        from cw.cli import _WAIT_EXIT_BLOCKED, _WAIT_EXIT_FAILED, _WAIT_EXIT_TIMEOUT

        assert _WAIT_EXIT_FAILED == 1
        assert _WAIT_EXIT_BLOCKED == 2
        assert _WAIT_EXIT_TIMEOUT == 124


# ---------------------------------------------------------------------------
# TestDevQueueWaitSentinelAware (GitHub issue #535)
# ---------------------------------------------------------------------------


class TestDevQueueWaitSentinelAware:
    """Sentinel-aware ``cw dev-queue wait`` (GitHub issue #535).

    Tests for the inline poll loop that reads AUTO_DEV_RESULT sentinels
    from the transcript directly instead of relying solely on task-status
    polling.  Exercises TERMINAL, HEARTBEAT→TERMINAL, ATTENTION, and the
    spawn-window (session_id=None) grace path.
    """

    # --- shared sentinel text fixtures ---

    _SHIPPED_SENTINEL = (
        "<<<AUTO_DEV_RESULT\n"
        "{\n"
        '  "schema_version": 2,\n'
        '  "ticket_id": "535",\n'
        '  "status": "shipped",\n'
        '  "stage_reached": "stage5_post_create",\n'
        '  "scope": {"tier": "small", "files": 2, "lines_estimate": 30,\n'
        '    "lines_actual": 25, "forbidden_touched": false},\n'
        '  "plan_source": "linear_existing",\n'
        '  "branch": "dev/535-fix",\n'
        '  "worktree_path": null,\n'
        '  "fork_point_sha": "abc535",\n'
        '  "commits": ["def535"],\n'
        '  "pr": {"number": 535, '
        '"url": "https://github.com/foo/bar/pull/535", '
        '"auto_merge": true, "base": "main"},\n'
        '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
        '  "health": {"lowest_agent_confidence": "HIGH",'
        ' "any_incomplete_risk": false,\n'
        '    "shortcuts": [], "recommendation": "PROCEED",\n'
        '    "downgrade_applied": false,'
        ' "fix_loop_escalated": false},\n'
        '  "friction_highlights": [],\n'
        '  "blocker": null,\n'
        '  "next_actions": []\n'
        "}\n"
        "AUTO_DEV_RESULT>>>"
    )

    def _write_transcript(
        self,
        worktree: Path,
        claude_session_id: str,
        assistant_text: str,
        fake_home: Path,
    ) -> Path:
        """Write a transcript JSONL file and return the transcript path."""

        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        }
        transcript = project_dir / f"{claude_session_id}.jsonl"
        transcript.write_text(json.dumps(record) + "\n")
        return transcript

    def _seed_running_task(
        self,
        ticket_id: str,
        session_id: str | None,
        client: str = "genhealth",
    ) -> None:
        """Write a RUNNING TicketTask to the dev queue."""
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask

        store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client=client,
                    status=QueueItemStatus.RUNNING,
                    session_id=session_id,
                )
            ]
        )
        save_dev_queue(store)

    def _make_running_session(
        self,
        session_id: str,
        worktree: Path,
        claude_session_id: str | None = None,
        surface_ref: str = "abcd1234",
        started_at: datetime | None = None,
    ) -> Session:
        """Build an ACTIVE Session pointing at *worktree*."""
        from cw.models import SessionOrigin, SessionPurpose, SessionStatus

        return Session(
            id=session_id,
            name="genhealth/impl",
            client="genhealth",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            origin=SessionOrigin.DAEMON,
            workspace_path=worktree,
            worktree_path=worktree,
            surface_ref=surface_ref,
            claude_session_id=claude_session_id,
            started_at=started_at or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

    def test_terminal_shipped_csid_set(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """TERMINAL: transcript has ``shipped`` sentinel, csid known → exit 0."""
        import json as _json

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-535"
        worktree.mkdir(parents=True)

        session_id = "sess535a"
        csid = "uuid-535a-csid-set-1234"
        self._write_transcript(worktree, csid, self._SHIPPED_SENTINEL, fake_home)
        self._seed_running_task("GEN-535", session_id)

        session = self._make_running_session(
            session_id, worktree, claude_session_id=csid
        )
        state = CwState(sessions=[session])
        from cw.config import save_state as _save_state

        _save_state(state)

        # No real sleep/monotonic needed — sentinel found on first poll.
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-535", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip())
        assert payload["state"] == "terminal"
        assert payload["sentinel_status"] == "shipped"
        assert payload["pr_url"] == "https://github.com/foo/bar/pull/535"
        assert payload["ticket_id"] == "GEN-535"

    def test_terminal_shipped_csid_none_resolved_via_glob(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """TERMINAL: csid=None on Session → resolved via surface_ref glob."""
        import json as _json

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-535b"
        worktree.mkdir(parents=True)

        session_id = "sess535b"
        surface_ref = "abcd5350"  # 8-char hex
        # csid starts with surface_ref (as per _csid_from_transcript glob)
        csid = f"{surface_ref}-longer-uuid-suffix"

        # Transcript written AFTER session.started_at so the mtime guard passes.
        transcript = self._write_transcript(
            worktree, csid, self._SHIPPED_SENTINEL, fake_home
        )
        # Ensure mtime is fresh (after started_at = epoch).
        import os

        os.utime(transcript, None)

        self._seed_running_task("GEN-535B", session_id)

        started = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        session = self._make_running_session(
            session_id,
            worktree,
            claude_session_id=None,  # not yet set — will resolve via glob
            surface_ref=surface_ref,
            started_at=started,
        )
        state = CwState(sessions=[session])
        from cw.config import save_state as _save_state

        _save_state(state)

        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-535B", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip())
        assert payload["state"] == "terminal"
        assert payload["sentinel_status"] == "shipped"

    def test_heartbeat_then_terminal(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """HEARTBEAT → TERMINAL: first poll no sentinel, second poll shipped."""
        import json as _json

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-535c"
        worktree.mkdir(parents=True)

        session_id = "sess535c"
        csid = "uuid-535c"
        self._seed_running_task("GEN-535C", session_id)

        started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        session = self._make_running_session(
            session_id, worktree, claude_session_id=csid, started_at=started
        )
        state = CwState(sessions=[session])
        from cw.config import save_state as _save_state

        _save_state(state)

        # First call: transcript exists (fresh, no sentinel) → keep polling.
        # Second call: transcript has shipped sentinel → TERMINAL.
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        transcript = project_dir / f"{csid}.jsonl"

        call_count = [0]

        def _fake_sentinel(cwd: str, claude_session_id: str | None) -> object:
            """Return None on first call, AutoDevResult on second."""
            from cw.auto_dev_result import parse_stdout

            call_count[0] += 1
            if call_count[0] == 1:
                # Write a fresh-mtime transcript (no sentinel) to prevent ATTENTION.
                transcript.write_text(json.dumps({"type": "user"}) + "\n")
                return None
            # Second poll: inject the shipped sentinel.
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": self._SHIPPED_SENTINEL}
                            ]
                        },
                    }
                )
                + "\n"
            )
            return parse_stdout(self._SHIPPED_SENTINEL)

        monkeypatch.setattr(
            "cw.cli.dev_queue._parse_sentinel_from_transcript", _fake_sentinel
        )
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)

        # monotonic: first poll returns 0.0 (under deadline=300).
        # After first sleep, return 10.0 (still under deadline).
        monotonic_values = iter([0.0, 0.0, 10.0, 10.0])
        monkeypatch.setattr(
            "cw.cli.dev_queue.time.monotonic", lambda: next(monotonic_values, 10.0)
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-535C", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip())
        assert payload["state"] == "terminal"
        assert payload["sentinel_status"] == "shipped"
        assert call_count[0] == 2

    def test_attention_stale_not_in_roster(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """ATTENTION: stale + no sentinel + not in roster → exit 3."""
        import json as _json

        from cw.cli import _WAIT_EXIT_ATTENTION

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-535d"
        worktree.mkdir(parents=True)

        session_id = "sess535d"
        surface_ref = "deadbeef"  # 8-char hex
        csid = "uuid-535d"

        self._seed_running_task("GEN-535D", session_id)

        # Session started at T=0; transcript last-written at T=1 (well within history).
        # freeze_time at T=9999 makes the transcript very stale.
        started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        session = self._make_running_session(
            session_id,
            worktree,
            claude_session_id=csid,
            surface_ref=surface_ref,
            started_at=started,
        )
        state = CwState(sessions=[session])
        from cw.config import save_state as _save_state

        _save_state(state)

        # Write a stale transcript (no sentinel).
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        transcript = project_dir / f"{csid}.jsonl"
        transcript.write_text(json.dumps({"type": "user"}) + "\n")

        # Freeze time at a point far in the future relative to the transcript mtime.
        # The transcript mtime will be "now" (real time), but with freeze_time the
        # datetime.now(UTC) call inside _transcript_age_seconds will return a future
        # time, making the age appear large.
        #
        # Strategy: mock _transcript_age_seconds to return a large value directly,
        # and mock _parse_sentinel_from_transcript to return None.
        monkeypatch.setattr(
            "cw.cli.dev_queue._parse_sentinel_from_transcript", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue._transcript_age_seconds",
            lambda *_a, **_kw: 99999.0,  # very stale
        )

        # Daemon roster does NOT contain surface_ref.
        from cw.native_daemon import FakeNativeDaemonClient

        fake_daemon = FakeNativeDaemonClient()
        # Don't add surface_ref to roster → not in roster.
        monkeypatch.setattr(
            "cw.cli.dev_queue.get_native_daemon_client", lambda: fake_daemon
        )

        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-535D", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == _WAIT_EXIT_ATTENTION, result.output
        payload = _json.loads(result.output.strip())
        assert payload["state"] == "attention"
        assert payload["sentinel_status"] is None

    def test_spawn_window_session_id_none_no_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Spawn-window: session_id=None → keeps polling, never fires ATTENTION."""
        from cw.cli import _WAIT_EXIT_TIMEOUT

        self._seed_running_task("GEN-535E", session_id=None)

        # monotonic: first poll → 0.0; second call (deadline check) → 9999.0 (expired).
        monotonic_calls = iter([0.0, 9999.0])
        monkeypatch.setattr(
            "cw.cli.dev_queue.time.monotonic", lambda: next(monotonic_calls, 9999.0)
        )
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-535E", "--client", "genhealth"]
        )
        # Should timeout (124), NOT attention (3) — spawn-window grace.
        assert result.exit_code == _WAIT_EXIT_TIMEOUT, result.output

    def test_reaped_mid_wait_exit_attention(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Reap mid-wait: session_id transitions non-None→None → exit 3 (#542).

        First poll sees RUNNING task with session_id set (spawn-window grace
        does NOT apply — session is registered).  Between polls, reconcile
        reverts the task to PENDING and clears session_id.  The wait must
        detect the non-None→None transition and surface ATTENTION rather than
        riding to the --timeout ceiling.
        """
        import json as _json

        from cw.cli import _WAIT_EXIT_ATTENTION
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask

        ticket_id = "GEN-542"
        session_id = "sess542-reap"
        surface_ref = "abcd5420"

        # Seed a running session in state so the first poll has somewhere to look.
        worktree = tmp_path / "wt" / "auto-dev-542"
        worktree.mkdir(parents=True)
        session = self._make_running_session(
            session_id, worktree, claude_session_id="uuid-542", surface_ref=surface_ref
        )
        from cw.config import save_state as _save_state

        _save_state(CwState(sessions=[session]))

        # load_dev_queue: first call → RUNNING+session_id; second → PENDING+None.
        call_count = [0]

        def _fake_load() -> DevQueueStore:
            call_count[0] += 1
            first = call_count[0] <= 1
            status = QueueItemStatus.RUNNING if first else QueueItemStatus.PENDING
            sid = session_id if first else None
            return DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="genhealth",
                        status=status,
                        session_id=sid,
                    )
                ]
            )

        monkeypatch.setattr("cw.cli.dev_queue.load_dev_queue", _fake_load)

        # No sentinel on the first poll (transcript hasn't finished writing yet).
        monkeypatch.setattr(
            "cw.cli.dev_queue._parse_sentinel_from_transcript", lambda *_a, **_kw: None
        )
        # Fresh transcript → not stale → normal ATTENTION predicate won't fire.
        monkeypatch.setattr(
            "cw.cli.dev_queue._transcript_age_seconds", lambda *_a, **_kw: 10.0
        )
        # Session in daemon roster → normal ATTENTION predicate won't fire.
        from cw.native_daemon import FakeNativeDaemonClient

        fake_daemon = FakeNativeDaemonClient()
        fake_daemon._live.add(surface_ref)
        monkeypatch.setattr(
            "cw.cli.dev_queue.get_native_daemon_client", lambda: fake_daemon
        )

        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        # Monotonic call sequence:
        #   call 1 — deadline init (0.0 → deadline=300)
        #   call 2 — first-poll _raise_if_deadline_exceeded (0.0 → not expired)
        #   call 3 — second-poll spawn-window deadline check (9999 → expired,
        #             only reached when the fix is NOT applied; with the fix
        #             _handle_reaped_mid_wait raises before this is needed).
        monotonic_calls = iter([0.0, 0.0, 9999.0])
        monkeypatch.setattr(
            "cw.cli.dev_queue.time.monotonic", lambda: next(monotonic_calls, 9999.0)
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", ticket_id, "--client", "genhealth", "--json"],
        )
        assert result.exit_code == _WAIT_EXIT_ATTENTION, result.output
        payload = _json.loads(result.output.strip())
        assert payload["state"] == "attention"
        assert payload["reason"] == "reaped_awaiting_redispatch"
        assert payload["session_id"] == session_id
        assert payload["status"] == "pending"
        assert payload["elapsed_seconds"] is None
        assert payload["transcript_age_seconds"] is None

    def test_wait_attention_exit_code_constant(self) -> None:
        """_WAIT_EXIT_ATTENTION constant equals 3."""
        from cw.cli import _WAIT_EXIT_ATTENTION

        assert _WAIT_EXIT_ATTENTION == 3

    def test_wait_json_terminal_via_queue_status_has_new_keys(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json COMPLETED queue status includes state/sentinel_status/pr_url."""
        import json as _json

        from cw.models import QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="GEN-536",
            client="genhealth",
            status=QueueItemStatus.COMPLETED,
            session_id="sess-536",
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue.wait_for_terminal",
            lambda _ticket_id, _client, **_kw: task,
        )
        # Seed so load_dev_queue finds it on the fast-path.
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore

        save_dev_queue(DevQueueStore(tasks=[task]))

        # No sleep/monotonic needed — fast path fires immediately.
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-536", "--client", "genhealth", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip())
        # Backward-compat: old keys still present.
        assert payload["ticket_id"] == "GEN-536"
        assert payload["client"] == "genhealth"
        assert payload["status"] == "completed"
        assert payload["session_id"] == "sess-536"
        # New keys present.
        assert payload["state"] == "terminal"
        assert "sentinel_status" in payload
        assert "pr_url" in payload

    def test_terminal_non_json_output(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """TERMINAL via sentinel without --json emits human-readable line."""
        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-535f"
        worktree.mkdir(parents=True)

        session_id = "sess535f"
        csid = "uuid-535f"
        self._write_transcript(worktree, csid, self._SHIPPED_SENTINEL, fake_home)
        self._seed_running_task("GEN-535F", session_id)

        session = self._make_running_session(
            session_id, worktree, claude_session_id=csid
        )
        from cw.config import save_state as _save_state

        _save_state(CwState(sessions=[session]))

        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "wait", "GEN-535F", "--client", "genhealth"],
        )
        assert result.exit_code == 0, result.output
        assert "SHIPPED" in result.output

    def test_attention_non_json_output(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """ATTENTION without --json emits human-readable line."""
        from cw.cli import _WAIT_EXIT_ATTENTION

        worktree = tmp_path / "wt" / "auto-dev-535g"
        worktree.mkdir(parents=True)
        session_id = "sess535g"
        surface_ref = "cafe1234"

        self._seed_running_task("GEN-535G", session_id)
        session = self._make_running_session(
            session_id, worktree, surface_ref=surface_ref
        )
        from cw.config import save_state as _save_state

        _save_state(CwState(sessions=[session]))

        monkeypatch.setattr(
            "cw.cli.dev_queue._parse_sentinel_from_transcript", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.cli.dev_queue._transcript_age_seconds", lambda *_a, **_kw: 99999.0
        )
        from cw.native_daemon import FakeNativeDaemonClient

        _fake_daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.cli.dev_queue.get_native_daemon_client", lambda: _fake_daemon
        )
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)
        monkeypatch.setattr("cw.cli.dev_queue.time.monotonic", lambda: 0.0)

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-535G", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_ATTENTION
        assert "ATTENTION" in result.output

    def test_heartbeat_timeout_path(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """HEARTBEAT: transcript fresh, hard ceiling hit → exit 124."""
        from cw.cli import _WAIT_EXIT_TIMEOUT

        worktree = tmp_path / "wt" / "auto-dev-535h"
        worktree.mkdir(parents=True)
        session_id = "sess535h"
        surface_ref = "1234cafe"  # 8-char hex

        self._seed_running_task("GEN-535H", session_id)
        session = self._make_running_session(
            session_id, worktree, surface_ref=surface_ref
        )
        from cw.config import save_state as _save_state

        _save_state(CwState(sessions=[session]))

        monkeypatch.setattr(
            "cw.cli.dev_queue._parse_sentinel_from_transcript", lambda *_a, **_kw: None
        )
        # Fresh transcript (not stale) → no ATTENTION.
        monkeypatch.setattr(
            "cw.cli.dev_queue._transcript_age_seconds", lambda *_a, **_kw: 5.0
        )
        from cw.native_daemon import FakeNativeDaemonClient

        _fake_d = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.cli.dev_queue.get_native_daemon_client", lambda: _fake_d
        )
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)

        # First monotonic() call (deadline init): 0.0.
        # Subsequent calls (deadline checks): first returns 0.0 (alive),
        # then 9999.0 (deadline exceeded after heartbeat).
        mono_values = iter([0.0, 0.0, 9999.0])
        monkeypatch.setattr(
            "cw.cli.dev_queue.time.monotonic", lambda: next(mono_values, 9999.0)
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-535H", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_TIMEOUT, result.output

    def test_transcript_age_seconds_no_project_dir(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        """_transcript_age_seconds returns None when worktree_path is None."""
        from cw.cli import _transcript_age_seconds

        session = Session(
            id="test-age-1",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=tmp_path,
            worktree_path=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = _transcript_age_seconds(session, datetime.now(UTC))
        assert result is None

    def test_transcript_age_seconds_no_transcript_file(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_transcript_age_seconds returns None when transcript file is missing."""
        from cw.cli import _transcript_age_seconds

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "no-transcript"
        worktree.mkdir(parents=True)

        session = Session(
            id="test-age-2",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=worktree,
            worktree_path=worktree,
            claude_session_id="missing-csid",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # project_dir will exist (fake_home/.claude/projects/...) but transcript won't.
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)

        result = _transcript_age_seconds(session, datetime.now(UTC))
        assert result is None

    def test_transcript_age_seconds_no_csid_no_candidates(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_transcript_age_seconds with no csid and no .jsonl files returns None."""
        from cw.cli import _transcript_age_seconds

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "no-candidates"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)

        session = Session(
            id="test-age-3",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=worktree,
            worktree_path=worktree,
            claude_session_id=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = _transcript_age_seconds(session, datetime.now(UTC))
        assert result is None

    def test_transcript_age_seconds_stale_mtime_guard(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_transcript_age_seconds returns None when transcript mtime <= started_at."""
        from cw.cli import _transcript_age_seconds

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "stale-mtime"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)

        # Write a .jsonl file then set its mtime to BEFORE started_at.
        transcript = project_dir / "someid.jsonl"
        transcript.write_text("{}\n")
        import os

        # started_at = 2026-06-11T12:00:00 UTC; set mtime to 2026-01-01 (before).
        os.utime(transcript, (1735689600.0, 1735689600.0))

        started = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
        session = Session(
            id="test-age-4",
            name="c/impl",
            client="c",
            purpose=SessionPurpose.IMPL,
            workspace_path=worktree,
            worktree_path=worktree,
            claude_session_id=None,
            started_at=started,
        )
        result = _transcript_age_seconds(session, datetime.now(UTC))
        assert result is None

    def test_spawn_window_session_not_in_state_polls_then_times_out(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """session_id set but session not in CwState → poll, eventually timeout."""
        from cw.cli import _WAIT_EXIT_TIMEOUT

        self._seed_running_task("GEN-535I", session_id="orphan-sess")
        # State has no sessions — session not found.
        from cw.config import save_state as _save_state

        _save_state(CwState(sessions=[]))

        # First call for deadline init: 0.0; sleep→continue loop;
        # second deadline check (inside session-None branch): 9999.0 → timeout.
        mono_values = iter([0.0, 9999.0])
        monkeypatch.setattr(
            "cw.cli.dev_queue.time.monotonic", lambda: next(mono_values, 9999.0)
        )
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "wait", "GEN-535I", "--client", "genhealth"]
        )
        assert result.exit_code == _WAIT_EXIT_TIMEOUT


# ---------------------------------------------------------------------------
# TestDevQueueWaitDuplicateResolution (GitHub issue #579)
# ---------------------------------------------------------------------------


class TestDevQueueWaitDuplicateResolution:
    """Fast-path uses _find_ticket to prefer live task over stale terminal."""

    def test_wait_resolves_live_task_over_cancelled(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CANCELLED + RUNNING for same ticket — fast-path must not short-circuit.

        With --timeout 0, a non-terminal task triggers exit 124 (timeout).
        If the fast-path mistakenly binds to CANCELLED it would exit 1.
        """
        from datetime import UTC, datetime, timedelta

        from cw.cli import _WAIT_EXIT_TIMEOUT
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask

        old_ts = datetime(2025, 5, 1, tzinfo=UTC)
        new_ts = old_ts + timedelta(hours=1)

        store = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="GEN-579C",
                    client="genhealth",
                    status=QueueItemStatus.CANCELLED,
                    created_at=old_ts,
                ),
                TicketTask(
                    ticket_id="GEN-579C",
                    client="genhealth",
                    status=QueueItemStatus.RUNNING,
                    session_id="sess-live",
                    created_at=new_ts,
                ),
            ]
        )
        save_dev_queue(store)

        # Prevent consume_completed_sessions from doing real dispatch work
        monkeypatch.setattr("cw.dev_queue.consume_completed_sessions", lambda: 0)
        monkeypatch.setattr("cw.cli.dev_queue.time.sleep", lambda _: None)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dev-queue",
                "wait",
                "GEN-579C",
                "--client",
                "genhealth",
                "--timeout",
                "0",
            ],
        )
        # exit 124 = timeout hit on non-terminal task (RUNNING was found)
        # exit 1   = CANCELLED was resolved as the fast-path result (the bug)
        assert result.exit_code == _WAIT_EXIT_TIMEOUT, (
            f"Expected timeout (124) but got {result.exit_code}.\n"
            f"Output: {result.output}"
        )


class TestResultValidate:
    def _valid_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "ticket_id": "GEN-1234",
            "status": "shipped",
            "stage_reached": "stage5_post_create",
            "scope": {
                "tier": "small",
                "files": 3,
                "lines_estimate": 42,
                "lines_actual": 47,
                "forbidden_touched": False,
            },
            "plan_source": "linear_existing",
            "branch": "dev/gen-1234-fix-login",
            "worktree_path": "/tmp/wt/gen-1234",
            "fork_point_sha": "abc1234",
            "commits": ["sha1", "sha2"],
            "pr": {
                "number": 42,
                "url": "https://github.com/foo/bar/pull/42",
                "auto_merge": True,
                "base": "main",
            },
            "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "MEDIUM",
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": None,
            "next_actions": ["wait_for_ci"],
        }

    def test_valid_json_stdin_exits_zero_with_normalized_json(self) -> None:
        runner = CliRunner()
        valid_json = json.dumps(self._valid_payload())
        result = runner.invoke(main, ["result", "validate", "-"], input=valid_json)
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["status"] == "shipped"

    def test_invalid_json_stdin_exits_nonzero_with_error_in_output(self) -> None:
        runner = CliRunner()
        bad_payload = json.dumps({"status": "shipped", "schema_version": 1})
        result = runner.invoke(main, ["result", "validate", "-"], input=bad_payload)
        assert result.exit_code != 0
        assert len(result.output) > 0

    def test_malformed_json_stdin_exits_nonzero(self) -> None:
        runner = CliRunner()
        bad_input = "not-valid-json{"
        result = runner.invoke(main, ["result", "validate", "-"], input=bad_input)
        assert result.exit_code != 0
        assert "json:" in result.output

    def test_valid_json_file_path_exits_zero(self, tmp_path: Path) -> None:
        import pathlib

        payload_file = pathlib.Path(tmp_path) / "payload.json"
        payload_file.write_text(json.dumps(self._valid_payload()))
        runner = CliRunner()
        result = runner.invoke(main, ["result", "validate", str(payload_file)])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["status"] == "shipped"


# ---------------------------------------------------------------------------
# cw doctor --reap <SESSION> targeted reap tests (GitHub #555)
# ---------------------------------------------------------------------------


class TestDoctorTargetedReap:
    """cw doctor --reap <SESSION> reaps a specific session by id or name."""

    def test_targeted_reap_by_id(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--reap <id> reaps session and exits 0."""
        from pathlib import Path as _Path

        from cw.config import load_state, save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import (
            CwState,
            DevQueueStore,
            QueueItemStatus,
            SessionOrigin,
            SessionPurpose,
            SessionStatus,
            TicketTask,
        )
        from cw.native_daemon import FakeNativeDaemonClient

        monkeypatch.setattr(
            "cw.doctor.get_native_daemon_client", FakeNativeDaemonClient
        )

        from cw.models import Session

        sess = Session(
            id="cli-reap-1",
            name="client-a/auto-dev/cli-ticket-1",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            origin=SessionOrigin.DAEMON,
            workspace_path=_Path("/tmp/ws"),
        )
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id="cli-ticket-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="cli-reap-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--reap", "cli-reap-1"])

        assert result.exit_code == 0, result.output
        state = load_state()
        updated = next(s for s in state.sessions if s.id == "cli-reap-1")
        assert updated.status == SessionStatus.COMPLETED
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "cli-ticket-1")
        assert t.status == QueueItemStatus.PENDING

    def test_targeted_reap_not_found_exits_1(self, tmp_config_dir: Path) -> None:
        """--reap <non-existent> exits 1 with error message."""
        from cw.config import save_state
        from cw.dev_queue import save_dev_queue
        from cw.models import CwState, DevQueueStore

        save_state(CwState(sessions=[]))
        save_dev_queue(DevQueueStore(tasks=[]))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--reap", "no-such-session"])

        assert result.exit_code == 1
        assert "no-such-session" in result.output

    def test_regression_542_doctor_reap_without_session_still_works(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--reap without SESSION arg invokes run_doctor (no regression)."""
        from cw.doctor import CheckResult

        monkeypatch.setattr(
            "cw.doctor._check_claude_version",
            lambda: CheckResult("claude-version", ok=True, detail="stubbed"),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--reap"])

        # exits 0 on healthy env (stub ensures claude version check passes)
        assert result.exit_code == 0, result.output

    def test_session_arg_without_reap_flag_warns(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SESSION arg without --reap emits a warning on stderr."""
        from cw.doctor import CheckResult

        monkeypatch.setattr(
            "cw.doctor._check_claude_version",
            lambda: CheckResult("claude-version", ok=True, detail="stubbed"),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "some-session-id"])

        assert "SESSION argument has no effect without --reap" in result.output


# ---------------------------------------------------------------------------
# TestLaneCli — cw lane ls / add / rm / pause / resume
# ---------------------------------------------------------------------------


def _write_clients_yaml_with_lanes(
    tmp_config_dir: Path,
    tmp_path: Path,
    client_name: str,
    lanes: list[str],
) -> None:
    """Write clients.yaml with named lanes for a client."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    lane_yaml = "".join(
        f"      - name: {ln}\n        max_parallel: 1\n" for ln in lanes
    )
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  {client_name}:\n    workspace_path: {ws}\n    lanes:\n{lane_yaml}"
    )


def _write_clients_yaml_no_lanes(
    tmp_config_dir: Path,
    tmp_path: Path,
    client_name: str,
) -> None:
    """Write clients.yaml without explicit lanes (uses default)."""
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        f"clients:\n  {client_name}:\n    workspace_path: {ws}\n"
    )


class TestLaneLs:
    def test_lane_ls_lists_declared_lanes(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "urgent"]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["lane", "ls", "acme"])
        assert result.exit_code == 0, result.output
        assert "default" in result.output
        assert "urgent" in result.output

    def test_lane_ls_json_output(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "urgent"]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["lane", "ls", "acme", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        names = [item["name"] for item in data]
        assert "default" in names
        assert "urgent" in names


class TestLaneAdd:
    def test_lane_add_writes_clients_yaml(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_no_lanes(tmp_config_dir, tmp_path, "acme")

        runner = CliRunner()
        result = runner.invoke(main, ["lane", "add", "acme", "fast"])
        assert result.exit_code == 0, result.output
        assert "fast" in result.output

        from cw.config import load_clients

        client = load_clients()["acme"]
        lane_names = [ln.name for ln in client.effective_lanes]
        assert "fast" in lane_names

    def test_lane_add_emits_lane_created_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_no_lanes(tmp_config_dir, tmp_path, "acme")
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        runner = CliRunner()
        result = runner.invoke(main, ["lane", "add", "acme", "fast"])
        assert result.exit_code == 0, result.output

        events = read_events()
        lane_events = [
            e for e in events if e.type == OrchestratorEventType.LANE_CREATED
        ]
        assert len(lane_events) == 1
        assert lane_events[0].payload["lane"] == "fast"
        assert lane_events[0].payload["client"] == "acme"

    def test_lane_add_duplicate_hard_fails(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(tmp_config_dir, tmp_path, "acme", ["default"])
        runner = CliRunner()
        result = runner.invoke(main, ["lane", "add", "acme", "default"])
        assert result.exit_code != 0
        assert "already" in result.output.lower() or "exists" in result.output.lower()


class TestLaneRm:
    def test_lane_rm_removes_from_clients_yaml(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "fast"]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["lane", "rm", "acme", "fast"])
        assert result.exit_code == 0, result.output

        from cw.config import load_clients

        client = load_clients()["acme"]
        lane_names = [ln.name for ln in client.effective_lanes]
        assert "fast" not in lane_names

    def test_lane_rm_with_active_tasks_hard_fails(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "urgent"]
        )
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="ACM-1",
            client="acme",
            status=QueueItemStatus.PENDING,
            lane="urgent",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(main, ["lane", "rm", "acme", "urgent"])
        assert result.exit_code != 0


class TestLanePauseResume:
    def test_lane_pause_writes_override_and_emits_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "slow"]
        )
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        runner = CliRunner()
        result = runner.invoke(main, ["lane", "pause", "acme", "slow"])
        assert result.exit_code == 0, result.output

        events = read_events()
        paused_events = [
            e for e in events if e.type == OrchestratorEventType.LANE_PAUSED
        ]
        assert len(paused_events) == 1
        assert paused_events[0].payload["client"] == "acme"
        assert paused_events[0].payload["lane"] == "slow"

    def test_lane_resume_writes_override_and_emits_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "slow"]
        )
        from cw.events import read_events
        from cw.models import OrchestratorEventType

        runner = CliRunner()
        runner.invoke(main, ["lane", "pause", "acme", "slow"])
        result = runner.invoke(main, ["lane", "resume", "acme", "slow"])
        assert result.exit_code == 0, result.output

        events = read_events()
        resumed_events = [
            e for e in events if e.type == OrchestratorEventType.LANE_RESUMED
        ]
        assert len(resumed_events) == 1
        assert resumed_events[0].payload["client"] == "acme"
        assert resumed_events[0].payload["lane"] == "slow"


# ---------------------------------------------------------------------------
# TestConfigGroup — cw config / cw config show / cw config concurrency
# ---------------------------------------------------------------------------


class TestConfigGroup:
    def test_config_bare_shows_config(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw config (bare) calls show_config (backward compat)."""
        _write_clients_yaml_no_lanes(tmp_config_dir, tmp_path, "acme")
        runner = CliRunner()
        with patch("cw.cli.config_cmds.show_config") as mock_cfg:
            result = runner.invoke(main, ["config"])
            assert result.exit_code == 0, result.output
            mock_cfg.assert_called_once()

    def test_config_show_subcommand(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """cw config show calls show_config."""
        _write_clients_yaml_no_lanes(tmp_config_dir, tmp_path, "acme")
        runner = CliRunner()
        with patch("cw.cli.config_cmds.show_config") as mock_cfg:
            result = runner.invoke(main, ["config", "show"])
            assert result.exit_code == 0, result.output
            mock_cfg.assert_called_once()

    def test_config_concurrency_get(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """cw config concurrency get exits 0 and shows concurrency layers."""
        runner = CliRunner()
        result = runner.invoke(main, ["config", "concurrency", "get"])
        assert result.exit_code == 0, result.output
        # Should show declared/override/effective layers
        out = result.output.lower()
        assert "declared" in out or "effective" in out or "ceiling" in out

    def test_config_concurrency_get_json(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw config concurrency get --json emits valid JSON."""
        runner = CliRunner()
        result = runner.invoke(main, ["config", "concurrency", "get", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "declared" in data or "effective" in data

    def test_config_concurrency_set_max_parallel_clients(
        self, tmp_config_dir: Path
    ) -> None:
        """cw config concurrency set max_parallel_clients=4 writes override store."""
        from cw.config import load_effective_config

        runner = CliRunner()
        result = runner.invoke(
            main, ["config", "concurrency", "set", "max_parallel_clients=4"]
        )
        assert result.exit_code == 0, result.output

        effective = load_effective_config()
        assert effective.max_parallel_clients == 4

    def test_config_concurrency_clear_all(self, tmp_config_dir: Path) -> None:
        """cw config concurrency clear removes all overrides."""
        from cw.config import load_effective_config

        # Set a value first
        runner = CliRunner()
        runner.invoke(main, ["config", "concurrency", "set", "max_parallel_clients=3"])
        # Clear all
        result = runner.invoke(main, ["config", "concurrency", "clear"])
        assert result.exit_code == 0, result.output

        effective = load_effective_config()
        assert effective.max_parallel_clients is None

    def test_config_concurrency_clear_single_key(self, tmp_config_dir: Path) -> None:
        """cw config concurrency clear max_parallel_clients removes that key only."""
        from cw.config import load_effective_config

        runner = CliRunner()
        runner.invoke(main, ["config", "concurrency", "set", "max_parallel_clients=3"])
        result = runner.invoke(
            main, ["config", "concurrency", "clear", "max_parallel_clients"]
        )
        assert result.exit_code == 0, result.output

        effective = load_effective_config()
        assert effective.max_parallel_clients is None

    def test_config_concurrency_set_no_equals_fails(self, tmp_config_dir: Path) -> None:
        """cw config concurrency set without = in assignment exits non-zero."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["config", "concurrency", "set", "max_parallel_clients4"]
        )
        assert result.exit_code != 0
        assert "key=value" in result.output or "Expected" in result.output

    def test_config_concurrency_set_unknown_key_fails(
        self, tmp_config_dir: Path
    ) -> None:
        """cw config concurrency set unknown_key=1 exits non-zero."""
        runner = CliRunner()
        result = runner.invoke(main, ["config", "concurrency", "set", "unknown_key=1"])
        assert result.exit_code != 0
        assert (
            "Unknown concurrency key" in result.output or "Supported" in result.output
        )

    def test_config_concurrency_set_non_integer_fails(
        self, tmp_config_dir: Path
    ) -> None:
        """cw config concurrency set max_parallel_clients=abc exits non-zero."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["config", "concurrency", "set", "max_parallel_clients=abc"]
        )
        assert result.exit_code != 0
        assert "integer" in result.output or "must be" in result.output


# ---------------------------------------------------------------------------
# TestDevQueueAddLane — cw dev-queue add --lane
# ---------------------------------------------------------------------------


class TestDevQueueAddLane:
    def test_dev_queue_add_default_lane(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--lane default (implicit) routes to default lane."""
        _write_clients_yaml_with_lanes(tmp_config_dir, tmp_path, "acme", ["default"])
        from cw.dev_queue import load_dev_queue
        from cw.models import DEFAULT_LANE

        runner = CliRunner()
        result = runner.invoke(main, ["dev-queue", "add", "ACM-1", "-c", "acme"])
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "ACM-1")
        assert task.lane == DEFAULT_LANE

    def test_dev_queue_add_explicit_lane(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--lane fast routes to the fast lane."""
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "fast"]
        )
        from cw.dev_queue import load_dev_queue

        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "add", "ACM-2", "-c", "acme", "--lane", "fast"]
        )
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        task = next(t for t in store.tasks if t.ticket_id == "ACM-2")
        assert task.lane == "fast"

    def test_dev_queue_add_undeclared_lane_hard_fails(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """--lane undeclared exits non-zero with helpful message."""
        _write_clients_yaml_with_lanes(tmp_config_dir, tmp_path, "acme", ["default"])
        runner = CliRunner()
        result = runner.invoke(
            main, ["dev-queue", "add", "ACM-3", "-c", "acme", "--lane", "undeclared"]
        )
        assert result.exit_code != 0
        assert "undeclared" in result.output or "Lane" in result.output


# ---------------------------------------------------------------------------
# TestDevQueueMove — cw dev-queue move
# ---------------------------------------------------------------------------


class TestDevQueueMove:
    def test_dev_queue_move_pending_ticket(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw dev-queue move moves PENDING ticket and emits TICKET_MOVED event."""
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "fast"]
        )
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.events import read_events
        from cw.models import DevQueueStore, OrchestratorEventType, QueueItemStatus

        task = TicketTask(
            ticket_id="ACM-10",
            client="acme",
            status=QueueItemStatus.PENDING,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "move", "ACM-10", "-c", "acme", "--to", "fast"],
        )
        assert result.exit_code == 0, result.output

        store = load_dev_queue()
        moved = next(t for t in store.tasks if t.ticket_id == "ACM-10")
        assert moved.lane == "fast"

        events = read_events()
        moved_events = [
            e for e in events if e.type == OrchestratorEventType.TICKET_MOVED
        ]
        assert len(moved_events) == 1
        ev = moved_events[0]
        assert ev.payload["ticket_id"] == "ACM-10"
        assert ev.payload["from_lane"] == "default"
        assert ev.payload["to_lane"] == "fast"

    def test_dev_queue_move_running_task_hard_fails(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """cw dev-queue move on RUNNING task exits non-zero."""
        _write_clients_yaml_with_lanes(
            tmp_config_dir, tmp_path, "acme", ["default", "fast"]
        )
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus

        task = TicketTask(
            ticket_id="ACM-11",
            client="acme",
            status=QueueItemStatus.RUNNING,
            lane="default",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["dev-queue", "move", "ACM-11", "-c", "acme", "--to", "fast"],
        )
        assert result.exit_code != 0


def test_guide_exits_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["guide"])
    assert result.exit_code == 0


def test_guide_output_contains_markers() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["guide"])
    assert "orchestrating a sprint" in result.output
    assert "Sprint recipe" in result.output
    assert result.output.strip()


class TestBoardCommand:
    def test_board_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["board", "--help"])
        assert result.exit_code == 0
        assert "--once" in result.output
        assert "--interval" in result.output
        assert "--client" in result.output

    def test_board_once_frame_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cw board --once with empty state exits 0 and renders a frame."""
        import cw.board as board_module
        from cw.board import BoardState
        from cw.models import CwState, DevQueueStore, OrchestratorConfig

        fake_state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(),
            clients={},
            config=OrchestratorConfig(),
            now=datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC),
        )
        monkeypatch.setattr(board_module, "_load_board_state", lambda: fake_state)
        runner = CliRunner()
        result = runner.invoke(main, ["board", "--once"])
        assert result.exit_code == 0

    def test_board_once_frame_with_ticket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cw board --once with one ticket: MW-500 appears in output."""
        import cw.board as board_module
        from cw.board import BoardState
        from cw.models import (
            CwState,
            DevQueueStore,
            OrchestratorConfig,
            QueueItemStatus,
            Stage,
            TicketTask,
        )

        task = TicketTask(
            ticket_id="MW-500",
            client="acme",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        fake_state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={},
            config=OrchestratorConfig(),
            now=datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC),
        )
        monkeypatch.setattr(board_module, "_load_board_state", lambda: fake_state)
        runner = CliRunner()
        result = runner.invoke(main, ["board", "--once"])
        assert result.exit_code == 0
        assert "MW-500" in result.output

    def test_board_invokes_run_board(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """board command delegates to run_board."""
        from cw.cli import maintenance as cli_module

        called: list[dict[str, object]] = []
        monkeypatch.setattr(cli_module, "run_board", lambda **kw: called.append(kw))
        runner = CliRunner()
        result = runner.invoke(main, ["board", "--once"])
        assert result.exit_code == 0
        assert called
        assert called[0]["once"] is True
