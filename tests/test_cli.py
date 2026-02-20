"""Tests for cw.cli - Click CLI dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import (
    _complete_client,
    _complete_session,
    _display_sessions,
    _display_status,
    _parse_handoff_route,
    main,
)
from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestCli:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

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
                "my-client", "impl", worktree=None,
            )

    def test_start_with_purpose(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(main, ["start", "--purpose", "review", "my-client"])
            mock_start.assert_called_once_with(
                "my-client", "review", worktree=None,
            )

    def test_start_with_worktree(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_session") as mock_start:
            runner.invoke(
                main, ["start", "--worktree", "feat/search", "my-client"],
            )
            mock_start.assert_called_once_with(
                "my-client", "impl", worktree="feat/search",
            )

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
                "personal/debt", notify=None, auto=False,
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
                "my-session", cleanup=False, force=False,
            )

    def test_done_with_cleanup(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.done_session") as mock_done:
            runner.invoke(main, ["done", "my-session", "--cleanup", "--force"])
            mock_done.assert_called_once_with(
                "my-session", cleanup=True, force=True,
            )

    def test_done_no_session_arg(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.done_session") as mock_done:
            runner.invoke(main, ["done"])
            mock_done.assert_called_once_with(
                None, cleanup=False, force=False,
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
            "    workspace_path: /tmp/a\n"            "  beta:\n"
            "    workspace_path: /tmp/b\n"            "  apricot:\n"
            "    workspace_path: /tmp/c\n"        )

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
            "    workspace_path: /tmp/a\n"            "  beta:\n"
            "    workspace_path: /tmp/b\n"        )

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
                    name="test-client/review",
                    client="test-client",
                    purpose=SessionPurpose.REVIEW,
                    status=SessionStatus.COMPLETED,
                    workspace_path=sample_client.workspace_path,
                ),
            ]
        )
        save_state(state)

        items = _complete_session(None, None, "")  # type: ignore[arg-type]
        names = [item.value for item in items]
        assert "test-client/impl" in names
        assert "test-client/review" not in names

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
        capsys: pytest.CaptureFixture[str],
        mock_zellij: dict[str, list[Any]],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            "clients:\n"
            "  test-client:\n"
            "    workspace_path: /tmp/ws\n"            "  other-client:\n"
            "    workspace_path: /tmp/ws2\n"        )

        save_state(sample_state)

        _display_status()

        output = capsys.readouterr().out
        assert "Clients configured: 2" in output
        assert "Active sessions:    1" in output
        assert "Backgrounded:       1" in output

    def test_detects_crashed_session(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="crash001",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="impl",
                )
            ]
        )
        save_state(state)

        # Zellij session exists but impl pane has exited
        monkeypatch.setattr(
            "cw.zellij.session_exists", lambda _name: True
        )

        def _mock_check_pane_health(
            session: str | None = None, tab_name: str | None = None,
        ) -> dict[str, bool]:
            return {"impl": False, "review": True, "debt": True}

        monkeypatch.setattr(
            "cw.zellij.check_pane_health",
            _mock_check_pane_health,
        )

        _display_status()

        output = capsys.readouterr().out
        assert "crashed" in output.lower()
        assert "Active sessions:    0" in output

        # Verify state was persisted
        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED

    def test_crashed_session_shows_reason(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {sample_client.workspace_path}\n"
        )

        state = CwState(
            sessions=[
                Session(
                    id="crash002",
                    name="test-client/impl",
                    client="test-client",
                    purpose=SessionPurpose.IMPL,
                    status=SessionStatus.ACTIVE,
                    workspace_path=sample_client.workspace_path,
                    zellij_pane="impl",
                )
            ]
        )
        save_state(state)

        monkeypatch.setattr(
            "cw.zellij.session_exists", lambda _name: True
        )

        def _mock_check_pane_health(
            session: str | None = None, tab_name: str | None = None,
        ) -> dict[str, bool]:
            return {"impl": False}

        monkeypatch.setattr(
            "cw.zellij.check_pane_health",
            _mock_check_pane_health,
        )

        _display_status()

        output = capsys.readouterr().out
        assert "(crashed)" in output

        updated = load_state()
        assert updated.sessions[0].completed_reason == CompletionReason.CRASHED


class TestHandoffCli:
    def test_two_arg_route(self) -> None:
        src, tgt = _parse_handoff_route("impl", "review")
        assert src == "impl"
        assert tgt == "review"

    def test_arrow_route(self) -> None:
        src, tgt = _parse_handoff_route("impl->review", None)
        assert src == "impl"
        assert tgt == "review"

    def test_arrow_route_with_spaces(self) -> None:
        src, tgt = _parse_handoff_route("impl -> review", None)
        assert src == "impl"
        assert tgt == "review"

    def test_handoff_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.handoff_session") as mock_handoff:
            runner.invoke(main, ["handoff", "impl", "review"])
            mock_handoff.assert_called_once_with(
                "impl", "review", client_name=None,
            )

    def test_handoff_arrow_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.handoff_session") as mock_handoff:
            runner.invoke(main, ["handoff", "impl->review"])
            mock_handoff.assert_called_once_with(
                "impl", "review", client_name=None,
            )

    def test_handoff_with_client(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.handoff_session") as mock_handoff:
            runner.invoke(
                main, ["handoff", "impl", "review", "--client", "sigma"],
            )
            mock_handoff.assert_called_once_with(
                "impl", "review", client_name="sigma",
            )

    def test_missing_route_raises(self) -> None:
        with pytest.raises(CwError, match="Handoff requires"):
            _parse_handoff_route("impl", None)


class TestBgNotifyCli:
    def test_bg_with_notify(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg", "--notify", "review"])
            mock_bg.assert_called_once_with(None, notify="review", auto=False)

    def test_bg_with_notify_short(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.background_session") as mock_bg:
            runner.invoke(main, ["bg", "-n", "review"])
            mock_bg.assert_called_once_with(None, notify="review", auto=False)


class TestPlanCli:
    def test_plan_no_plans(
        self,
        tmp_config_dir: Path,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        ws = tmp_config_dir / "workspace"
        ws.mkdir()
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {ws}\n"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["plan", "test-client"])
        assert result.exit_code == 0
        assert "No plans found" in result.output

    def test_plan_shows_progress(
        self,
        tmp_config_dir: Path,
    ) -> None:
        clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
        ws = tmp_config_dir / "workspace"
        plans_dir = ws / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        clients_file.write_text(
            f"clients:\n"
            f"  test-client:\n"
            f"    workspace_path: {ws}\n"
        )

        (plans_dir / "test-plan.md").write_text(
            "# Test Plan\n\n"
            "## Phase 1\n\n"
            "- [x] Task A\n"
            "- [ ] Task B\n"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["plan", "test-client"])
        assert result.exit_code == 0
        assert "Test Plan" in result.output
        assert "1/2" in result.output
        assert "50%" in result.output

    def test_plan_unknown_client(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["plan", "nonexistent"])
        assert result.exit_code != 0


class TestDaemonCli:
    def test_daemon_start_with_client(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_daemon") as mock_start:
            runner.invoke(main, ["daemon", "start", "my-client"])
            mock_start.assert_called_once_with(
                "my-client", "debt",
                poll_interval=30, review=False,
            )

    def test_daemon_start_no_args_calls_all(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.start_daemon_all") as mock_all:
            runner.invoke(main, ["daemon", "start"])
            mock_all.assert_called_once_with(
                poll_interval=30, review=False,
            )

    def test_daemon_stop_with_client(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.stop_daemon") as mock_stop:
            runner.invoke(main, ["daemon", "stop", "my-client"])
            mock_stop.assert_called_once_with("my-client", "debt")

    def test_daemon_stop_no_args_stops_all(self) -> None:
        runner = CliRunner()
        with patch("cw.cli.stop_daemon") as mock_stop:
            runner.invoke(main, ["daemon", "stop"])
            mock_stop.assert_called_once_with("_all", "_all")
