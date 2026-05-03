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


# ---------------------------------------------------------------------------
# Linkage drift detection tests
# ---------------------------------------------------------------------------


class TestCheckLinkageHealthy:
    """Healthy states produce ok=True for all linkage check results."""

    def test_empty_state_all_linkage_ok(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
    ) -> None:
        """No sessions → no drift, all three linkage checks pass."""
        from cw.config import save_state
        from cw.models import CwState

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(CwState(sessions=[]))
        report = run_doctor()
        linkage_checks = [c for c in report.checks if c.name.startswith("linkage/")]
        assert len(linkage_checks) == 3
        assert all(c.ok for c in linkage_checks)

    def test_sessions_without_linkage_all_ok(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Sessions with no parent/worker fields → all linkage checks pass."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="sess-a",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                    ),
                    Session(
                        id="sess-b",
                        name="client/idea",
                        client="client",
                        purpose=SessionPurpose.IDEA,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                    ),
                ]
            )
        )
        report = run_doctor()
        linkage_checks = [c for c in report.checks if c.name.startswith("linkage/")]
        assert len(linkage_checks) == 3
        assert all(c.ok for c in linkage_checks)

    def test_valid_bidirectional_linkage_all_ok(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Orchestrator + workers with consistent bidirectional refs → all pass."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-1",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["worker-1", "worker-2"],
                    ),
                    Session(
                        id="worker-1",
                        name="client/worker-1",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="orch-1",
                    ),
                    Session(
                        id="worker-2",
                        name="client/worker-2",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="orch-1",
                    ),
                ]
            )
        )
        report = run_doctor()
        linkage_checks = [c for c in report.checks if c.name.startswith("linkage/")]
        assert len(linkage_checks) == 3
        assert all(c.ok for c in linkage_checks)


class TestCheckLinkageDanglingWorker:
    """Dangling worker: orchestrator lists a worker ID not in state."""

    def test_dangling_worker_flagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-1",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["worker-gone"],
                    ),
                ]
            )
        )
        report = run_doctor()
        dw = next(c for c in report.checks if c.name == "linkage/dangling-worker")
        assert not dw.ok
        assert "orch-1" in dw.detail
        assert "worker-gone" in dw.detail

    def test_dangling_worker_includes_remediation_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-2",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["missing-id"],
                    ),
                ]
            )
        )
        report = run_doctor()
        dw = next(c for c in report.checks if c.name == "linkage/dangling-worker")
        assert not dw.ok
        assert "remove" in dw.detail.lower() or "worker_session_ids" in dw.detail

    def test_multiple_dangling_workers_all_listed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Each missing worker ID appears in detail; the join doesn't drop entries."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-multi",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["gone-a", "gone-b", "gone-c"],
                    ),
                ]
            )
        )
        report = run_doctor()
        dw = next(c for c in report.checks if c.name == "linkage/dangling-worker")
        assert not dw.ok
        assert "gone-a" in dw.detail
        assert "gone-b" in dw.detail
        assert "gone-c" in dw.detail


class TestCheckLinkageDanglingParent:
    """Dangling parent: worker's parent_session_id is not in state."""

    def test_dangling_parent_flagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="worker-orphan",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="orch-gone",
                    ),
                ]
            )
        )
        report = run_doctor()
        dp = next(c for c in report.checks if c.name == "linkage/dangling-parent")
        assert not dp.ok
        assert "worker-orphan" in dp.detail
        assert "orch-gone" in dp.detail

    def test_dangling_parent_includes_remediation_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="worker-x",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="no-such-parent",
                    ),
                ]
            )
        )
        report = run_doctor()
        dp = next(c for c in report.checks if c.name == "linkage/dangling-parent")
        assert not dp.ok
        hint_words = {"parent_session_id", "restore", "clear"}
        assert any(w in dp.detail for w in hint_words)

    def test_multiple_dangling_parents_all_listed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Each worker referencing a missing parent appears in detail."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="worker-a",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="ghost-1",
                    ),
                    Session(
                        id="worker-b",
                        name="client/idea",
                        client="client",
                        purpose=SessionPurpose.IDEA,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="ghost-2",
                    ),
                ]
            )
        )
        report = run_doctor()
        dp = next(c for c in report.checks if c.name == "linkage/dangling-parent")
        assert not dp.ok
        assert "worker-a" in dp.detail
        assert "worker-b" in dp.detail
        assert "ghost-1" in dp.detail
        assert "ghost-2" in dp.detail


class TestCheckLinkageAsymmetric:
    """Asymmetric linkage: one side has the reference, other side doesn't."""

    def test_forward_only_asymmetry_flagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Orchestrator lists worker, but worker has no parent_session_id."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-f",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["worker-f"],
                    ),
                    Session(
                        id="worker-f",
                        name="client/worker-f",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        # parent_session_id deliberately absent
                    ),
                ]
            )
        )
        report = run_doctor()
        asym = next(c for c in report.checks if c.name == "linkage/asymmetric")
        assert not asym.ok
        assert "orch-f" in asym.detail
        assert "worker-f" in asym.detail

    def test_forward_only_wrong_parent_flagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Orchestrator lists worker, but worker points at a different parent."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-a",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["worker-q"],
                    ),
                    Session(
                        id="orch-b",
                        name="client/idea",
                        client="client",
                        purpose=SessionPurpose.IDEA,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                    ),
                    Session(
                        id="worker-q",
                        name="client/worker-q",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="orch-b",  # points at wrong orchestrator
                    ),
                ]
            )
        )
        report = run_doctor()
        asym = next(c for c in report.checks if c.name == "linkage/asymmetric")
        assert not asym.ok
        assert "orch-a" in asym.detail
        assert "worker-q" in asym.detail

    def test_reverse_only_asymmetry_flagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Worker claims parent, but parent's worker_session_ids omits the worker."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-r",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        # worker_session_ids deliberately empty
                    ),
                    Session(
                        id="worker-r",
                        name="client/worker-r",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        parent_session_id="orch-r",
                    ),
                ]
            )
        )
        report = run_doctor()
        asym = next(c for c in report.checks if c.name == "linkage/asymmetric")
        assert not asym.ok
        assert "worker-r" in asym.detail
        assert "orch-r" in asym.detail


class TestCheckLinkageIndependence:
    """Linkage checks are independent of other doctor sections."""

    def test_linkage_skipped_when_state_load_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
    ) -> None:
        """If state.json is corrupt, linkage checks are skipped (not crashed)."""
        from cw.config import state_file

        monkeypatch.setenv("CW_BACKEND", "fake")
        # Write corrupt JSON to the state file
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text("{not valid json}")

        # Doctor should not raise; linkage section simply absent
        report = run_doctor()
        linkage_checks = [c for c in report.checks if c.name.startswith("linkage/")]
        assert linkage_checks == []

    def test_drift_does_not_affect_other_checks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Dangling worker drift only fails the linkage check, not unrelated ones."""
        from cw.config import save_state
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        monkeypatch.setenv("CW_BACKEND", "fake")
        save_state(
            CwState(
                sessions=[
                    Session(
                        id="orch-iso",
                        name="client/impl",
                        client="client",
                        purpose=SessionPurpose.IMPL,
                        status=SessionStatus.ACTIVE,
                        workspace_path=sample_client.workspace_path,
                        worker_session_ids=["missing-worker"],
                    ),
                ]
            )
        )
        report = run_doctor()
        # The non-linkage checks (backend, config, state, dev-queue) are still ok
        non_linkage = [c for c in report.checks if not c.name.startswith("linkage/")]
        assert all(c.ok for c in non_linkage)
        # Linkage dangling-worker is not ok
        dw = next(c for c in report.checks if c.name == "linkage/dangling-worker")
        assert not dw.ok
