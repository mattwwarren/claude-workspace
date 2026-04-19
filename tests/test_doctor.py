"""Tests for cw.doctor — environment preflight checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw.cli import main
from cw.doctor import format_report, run_doctor
from cw.models import BackendName

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestRunDoctorFakeBackend:
    """When CW_BACKEND=fake the backend binary check is a no-op."""

    def test_resolved_backend_is_fake(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        report = run_doctor()
        assert report.backend is BackendName.FAKE
        assert report.ok

    def test_report_includes_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        report = run_doctor()
        assert report.version


class TestRunDoctorTmuxBackend:
    def test_tmux_missing_is_reported_as_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.doctor.shutil.which", lambda _name: None)
        report = run_doctor()
        assert not report.ok
        failing = [c for c in report.checks if not c.ok]
        assert any("tmux" in c.name for c in failing)

    def test_tmux_on_path_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.doctor.shutil.which", lambda _name: "/usr/bin/tmux")
        report = run_doctor()
        assert report.ok


class TestRunDoctorCmuxBackend:
    def test_cmux_non_darwin_is_reported_as_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "cmux")
        monkeypatch.setattr("cw.doctor.sys.platform", "linux")
        report = run_doctor()
        assert not report.ok
        failing = [c for c in report.checks if not c.ok]
        assert any("cmux" in c.name for c in failing)


class TestFormatReport:
    def test_render_contains_ok_and_fail_markers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.doctor.shutil.which", lambda _name: None)
        report = run_doctor()
        rendered = format_report(report)
        assert "FAIL" in rendered
        assert "status: problems detected" in rendered

    def test_healthy_report_ends_with_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        report = run_doctor()
        rendered = format_report(report)
        assert "status: healthy" in rendered


class TestDoctorCli:
    def test_cli_exit_zero_on_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "status: healthy" in result.output

    def test_cli_exit_nonzero_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "tmux")
        monkeypatch.setattr("cw.doctor.shutil.which", lambda _name: None)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code != 0
        assert "FAIL" in result.output
