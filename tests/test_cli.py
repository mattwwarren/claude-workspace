"""Tests for cw.cli - Click CLI dispatcher."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from cw.cli import main


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
        with patch("cw.session.list_sessions") as mock_list:
            runner.invoke(main, ["list"])
            mock_list.assert_called_once()

    def test_switch_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.zellij.go_to_tab") as mock_tab:
            runner.invoke(main, ["switch", "my-client"])
            mock_tab.assert_called_once_with("my-client")

    def test_status_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.session.show_status") as mock_status:
            runner.invoke(main, ["status"])
            mock_status.assert_called_once()

    def test_config_dispatches(self) -> None:
        runner = CliRunner()
        with patch("cw.config.show_config") as mock_config:
            runner.invoke(main, ["config"])
            mock_config.assert_called_once()

    def test_error_display(self) -> None:
        import click

        runner = CliRunner()
        with patch(
            "cw.session.start_session",
            side_effect=click.ClickException("Test error message"),
        ):
            result = runner.invoke(main, ["start", "bad-client"])
            assert result.exit_code != 0
            assert "Test error message" in result.output
