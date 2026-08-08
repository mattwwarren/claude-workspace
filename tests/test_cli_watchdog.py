"""Tests for the ``cw watchdog`` CLI group (RFC 0008 capstone, #1015)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.watchdog import WatchdogStatus, WatchdogTickResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestWatchdogTick:
    def test_delegates_to_run_tick(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_run_tick = MagicMock(
            return_value=WatchdogTickResult(
                escalated_ticket_ids=["GEN-1"],
                dispatch_loop_dead=False,
            )
        )
        monkeypatch.setattr("cw.watchdog.run_tick", mock_run_tick)

        result = runner.invoke(main, ["watchdog", "tick"])

        assert result.exit_code == 0
        mock_run_tick.assert_called_once_with()
        assert "GEN-1" in result.output
        assert "dispatch_loop_dead: False" in result.output
        # The park-marker-cycling check is gone with the process-kill
        # timeouts — the tick output must no longer advertise it.
        assert "cycling" not in result.output

    def test_reports_dispatch_dead(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.watchdog.run_tick",
            MagicMock(
                return_value=WatchdogTickResult(
                    escalated_ticket_ids=[],
                    dispatch_loop_dead=True,
                )
            ),
        )

        result = runner.invoke(main, ["watchdog", "tick"])

        assert result.exit_code == 0
        assert "dispatch_loop_dead: True" in result.output


class TestWatchdogInstall:
    def test_install_linux_prints_systemctl_command(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        service = tmp_path / "cw-watchdog.service"
        timer = tmp_path / "cw-watchdog.timer"
        monkeypatch.setattr(
            "cw.watchdog.install", MagicMock(return_value=[service, timer])
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")

        result = runner.invoke(main, ["watchdog", "install"])

        assert result.exit_code == 0
        assert str(service) in result.output
        assert str(timer) in result.output
        assert "systemctl --user" in result.output

    def test_install_macos_prints_launchctl_command(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plist = tmp_path / "com.cw.watchdog.plist"
        monkeypatch.setattr("cw.watchdog.install", MagicMock(return_value=[plist]))
        monkeypatch.setattr("platform.system", lambda: "Darwin")

        result = runner.invoke(main, ["watchdog", "install"])

        assert result.exit_code == 0
        assert f"launchctl load {plist}" in result.output


class TestWatchdogUninstall:
    def test_uninstall_reports_removed_paths(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        removed = [tmp_path / "cw-watchdog.service"]
        monkeypatch.setattr("cw.watchdog.uninstall", MagicMock(return_value=removed))

        result = runner.invoke(main, ["watchdog", "uninstall"])

        assert result.exit_code == 0
        assert str(removed[0]) in result.output

    def test_uninstall_reports_nothing_installed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.watchdog.uninstall", MagicMock(return_value=[]))

        result = runner.invoke(main, ["watchdog", "uninstall"])

        assert result.exit_code == 0
        assert "nothing installed" in result.output


class TestWatchdogStatus:
    def test_status_reports_installed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.watchdog.status",
            MagicMock(
                return_value=WatchdogStatus(
                    platform="linux",
                    installed=True,
                    paths=["/home/x/.config/systemd/user/cw-watchdog.service"],
                )
            ),
        )

        result = runner.invoke(main, ["watchdog", "status"])

        assert result.exit_code == 0
        assert "platform: linux" in result.output
        assert "installed: True" in result.output
        assert "cw-watchdog.service" in result.output

    def test_status_reports_not_installed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.watchdog.status",
            MagicMock(
                return_value=WatchdogStatus(
                    platform="darwin", installed=False, paths=["/x/plist"]
                )
            ),
        )

        result = runner.invoke(main, ["watchdog", "status"])

        assert result.exit_code == 0
        assert "installed: False" in result.output


class TestWatchdogGroupHelp:
    def test_group_is_registered(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["watchdog", "--help"])
        assert result.exit_code == 0
        assert "tick" in result.output
        assert "install" in result.output
        assert "uninstall" in result.output
        assert "status" in result.output
