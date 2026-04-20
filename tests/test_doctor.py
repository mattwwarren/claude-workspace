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

    from cw.models import ClientConfig


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


def test_run_doctor_reap_flag_reconciles_and_reports(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_doctor(reap=True) invokes reconcile and reports reaped sessions."""
    from cw.cmux import FakeCmuxAdapter
    from cw.config import load_state, save_state
    from cw.doctor import run_doctor
    from cw.models import CwState, Session, SessionPurpose, SessionStatus

    save_state(
        CwState(
            sessions=[
                Session(
                    id="phantom",
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

    def _adapter_with_decoy() -> FakeCmuxAdapter:
        # Non-empty live set bypasses reconcile's outage guard; "gone" still
        # isn't live so phantom is still reaped.
        a = FakeCmuxAdapter()
        a.spawn("decoy-ws", "echo")
        return a

    monkeypatch.setattr("cw.doctor.get_cmux_adapter", _adapter_with_decoy)

    report = run_doctor(reap=True)
    reap_checks = [c for c in report.checks if c.name == "reconciliation"]
    assert len(reap_checks) == 1
    assert reap_checks[0].ok is True
    detail = reap_checks[0].detail
    assert "phantom" in detail or "client-a/impl" in detail

    reloaded = load_state()
    phantom = reloaded.find_by_name_or_id("phantom")
    assert phantom is not None
    assert phantom.status == SessionStatus.COMPLETED


def test_cw_doctor_cli_reap_flag(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI `cw doctor --reap` forwards the flag."""
    from click.testing import CliRunner

    from cw.cli import main

    # Force fake backend so the binary/socket check passes on every platform
    # (macOS default is cmux, whose socket does not exist in CI).
    monkeypatch.setenv("CW_BACKEND", "fake")
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--reap"])
    assert result.exit_code == 0
    assert "reconciliation" in result.output
