"""Tests for cw.cli - Click CLI dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import _complete_client, _complete_session, _display_sessions, _display_status, main
from cw.config import save_state
from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus


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
        with patch("cw.session.start_session") as mock_start:
            runner.invoke(main, ["start", "my-client"])
            mock_start.assert_called_once_with("my-client", "impl")

    def test_start_with_purpose(self) -> None:
        runner = CliRunner()
        with patch("cw.session.start_session") as mock_start:
            runner.invoke(main, ["start", "--purpose", "review", "my-client"])
            mock_start.assert_called_once_with("my-client", "review")

    def test_bg_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.session.background_session") as mock_bg:
            runner.invoke(main, ["bg"])
            mock_bg.assert_called_once_with()

    def test_resume_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.session.resume_session") as mock_resume:
            runner.invoke(main, ["resume", "my-session"])
            mock_resume.assert_called_once_with("my-session")

    def test_list_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli._display_sessions") as mock_list:
            runner.invoke(main, ["list"])
            mock_list.assert_called_once()

    def test_switch_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.zellij.go_to_tab") as mock_tab:
            runner.invoke(main, ["switch", "my-client"])
            mock_tab.assert_called_once_with("my-client")

    def test_status_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.cli._display_status") as mock_status:
            runner.invoke(main, ["status"])
            mock_status.assert_called_once()

    def test_config_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.config.show_config") as mock_config:
            runner.invoke(main, ["config"])
            mock_config.assert_called_once()

    def test_error_display(self) -> None:
        from cw.exceptions import CwError

        runner = CliRunner()
        with patch(
            "cw.session.start_session",
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
        from cw.config import save_state

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
        from cw.config import save_state

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
        monkeypatch.setattr("cw.zellij.session_exists", lambda _name: True)
        monkeypatch.setattr(
            "cw.zellij.check_pane_health",
            lambda session=None: {"impl": False, "review": True, "debt": True},
        )

        _display_status()

        output = capsys.readouterr().out
        assert "crashed" in output.lower()
        assert "Active sessions:    0" in output

        # Verify state was persisted
        from cw.config import load_state

        updated = load_state()
        assert updated.sessions[0].status == SessionStatus.COMPLETED
