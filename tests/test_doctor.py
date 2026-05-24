"""Tests for cw.doctor — environment preflight checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw.cli import main
from cw.doctor import CheckResult, DoctorReport, format_report, run_doctor
from cw.models import BackendName

if TYPE_CHECKING:
    import pytest

    from cw.models import ClientConfig, Session


def _stub_claude_version_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub _check_claude_version to return ok=True.

    The real check runs ``claude --version``, which is not on the GH Actions
    runners. Healthy-path tests that assert ``report.ok`` (no failing checks)
    must neutralise the binary lookup; other tests covering this check
    directly remain in :class:`TestCheckClaudeVersionDirect`.
    """
    monkeypatch.setattr(
        "cw.doctor._check_claude_version",
        lambda: CheckResult("claude-version", ok=True, detail="2.1.139 (stubbed)"),
    )


class TestRunDoctorFakeBackend:
    """When CW_BACKEND=fake the backend binary check is a no-op."""

    def test_resolved_backend_is_fake(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        _stub_claude_version_ok(monkeypatch)
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
        _stub_claude_version_ok(monkeypatch)
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
        _stub_claude_version_ok(monkeypatch)
        report = run_doctor()
        rendered = format_report(report)
        assert "status: healthy" in rendered


class TestDoctorCli:
    def test_cli_exit_zero_on_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        monkeypatch.setenv("CW_BACKEND", "fake")
        _stub_claude_version_ok(monkeypatch)
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
    _stub_claude_version_ok(monkeypatch)
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

    def test_dangling_worker_flagged_with_remediation_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Single dangling worker: flag both IDs and surface remediation hint."""
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
        # Remediation hint is included alongside the IDs.
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
        assert "orch-multi" in dw.detail
        assert "gone-a" in dw.detail
        assert "gone-b" in dw.detail
        assert "gone-c" in dw.detail


class TestCheckLinkageDanglingParent:
    """Dangling parent: worker's parent_session_id is not in state."""

    def test_dangling_parent_flagged_with_remediation_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
    ) -> None:
        """Single dangling parent: flag both IDs and surface remediation hint."""
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
        # Remediation hint is included alongside the IDs.
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
        _stub_claude_version_ok(monkeypatch)
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


# ---------------------------------------------------------------------------
# Direct unit tests for _check_linkage (no run_doctor / state-file roundtrip)
# ---------------------------------------------------------------------------


def _mk_session(
    sid: str,
    workspace_path: Path,
    *,
    parent_session_id: str | None = None,
    worker_session_ids: list[str] | None = None,
) -> Session:
    """Build a minimal Session for linkage testing."""
    from cw.models import Session, SessionPurpose, SessionStatus

    return Session(
        id=sid,
        name=f"client/{sid}",
        client="client",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=workspace_path,
        parent_session_id=parent_session_id,
        worker_session_ids=worker_session_ids or [],
    )


class TestCheckLinkageDirect:
    """Direct tests of _check_linkage(state) — bypass state-file roundtrip."""

    def test_empty_state_returns_three_ok_results(self) -> None:
        """Empty state produces three named ok results."""
        from cw.doctor import _check_linkage
        from cw.models import CwState

        results = _check_linkage(CwState(sessions=[]))
        assert len(results) == 3
        assert all(r.ok for r in results)
        assert {r.name for r in results} == {
            "linkage/dangling-worker",
            "linkage/dangling-parent",
            "linkage/asymmetric",
        }

    def test_clean_bidirectional_linkage_passes(self, tmp_path: Path) -> None:
        """Orchestrator and worker referencing each other produce all-ok."""
        from cw.doctor import _check_linkage
        from cw.models import CwState

        state = CwState(
            sessions=[
                _mk_session("orch", tmp_path, worker_session_ids=["w1"]),
                _mk_session("w1", tmp_path, parent_session_id="orch"),
            ]
        )
        results = _check_linkage(state)
        assert all(r.ok for r in results)

    def test_dangling_worker_detected(self, tmp_path: Path) -> None:
        from cw.doctor import _check_linkage
        from cw.models import CwState

        state = CwState(
            sessions=[
                _mk_session("orch", tmp_path, worker_session_ids=["ghost"]),
            ]
        )
        results = {r.name: r for r in _check_linkage(state)}
        dw = results["linkage/dangling-worker"]
        assert not dw.ok
        assert "orch" in dw.detail
        assert "ghost" in dw.detail
        # Other two checks remain clean — drift is isolated to dangling-worker.
        # asymmetric stays ok because the forward check skips ghost workers
        # via `session_by_id.get(wid)` returning None (no session to inspect
        # for back-reference), so the missing-back-reference path never fires.
        assert results["linkage/dangling-parent"].ok
        assert results["linkage/asymmetric"].ok

    def test_dangling_parent_detected(self, tmp_path: Path) -> None:
        from cw.doctor import _check_linkage
        from cw.models import CwState

        state = CwState(
            sessions=[
                _mk_session("worker", tmp_path, parent_session_id="ghost-parent"),
            ]
        )
        results = {r.name: r for r in _check_linkage(state)}
        dp = results["linkage/dangling-parent"]
        assert not dp.ok
        assert "worker" in dp.detail
        assert "ghost-parent" in dp.detail
        assert results["linkage/dangling-worker"].ok
        assert results["linkage/asymmetric"].ok

    def test_multiple_danglers_all_listed(self, tmp_path: Path) -> None:
        """Mixed drift: multiple dangling workers and parents in one state."""
        from cw.doctor import _check_linkage
        from cw.models import CwState

        state = CwState(
            sessions=[
                _mk_session("orch", tmp_path, worker_session_ids=["gone-1", "gone-2"]),
                _mk_session("worker-a", tmp_path, parent_session_id="ghost-a"),
                _mk_session("worker-b", tmp_path, parent_session_id="ghost-b"),
            ]
        )
        results = {r.name: r for r in _check_linkage(state)}
        dw = results["linkage/dangling-worker"]
        dp = results["linkage/dangling-parent"]
        assert not dw.ok
        assert "gone-1" in dw.detail
        assert "gone-2" in dw.detail
        assert not dp.ok
        assert "ghost-a" in dp.detail
        assert "ghost-b" in dp.detail
        assert "worker-a" in dp.detail
        assert "worker-b" in dp.detail


# ---------------------------------------------------------------------------
# doctor --reap reverts completed-silent dev-queue tasks
# ---------------------------------------------------------------------------


def test_doctor_reap_reverts_completed_silent_dev_queue_task(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cw doctor --reap reverts a RUNNING task whose DAEMON COMPLETED session exists."""
    from datetime import UTC, datetime
    from pathlib import Path

    from click.testing import CliRunner

    from cw.cli import main
    from cw.cmux import FakeCmuxAdapter
    from cw.config import save_state
    from cw.dev_queue import load_dev_queue, save_dev_queue
    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        QueueItemStatus,
        Session,
        SessionOrigin,
        SessionPurpose,
        SessionStatus,
        TicketTask,
    )

    monkeypatch.setenv("CW_BACKEND", "fake")
    _stub_claude_version_ok(monkeypatch)

    comp_session = Session(
        id="comp-doctor-1",
        name="client-a/auto-dev/TKT-DR1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.COMPLETED,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        surface_ref=None,
        started_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    save_state(CwState(sessions=[comp_session]))

    task = TicketTask(
        ticket_id="TKT-DR1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="comp-doctor-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _fake_adapter() -> FakeCmuxAdapter:
        return FakeCmuxAdapter()

    monkeypatch.setattr("cw.doctor.get_cmux_adapter", _fake_adapter)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--reap"])
    assert result.exit_code == 0, result.output
    assert "reconciliation" in result.output
    assert "reverted 1 ticket(s)" in result.output

    store = load_dev_queue()
    t = next(t for t in store.tasks if t.ticket_id == "TKT-DR1")
    assert t.status == QueueItemStatus.PENDING
    assert t.session_id is None


# ---------------------------------------------------------------------------
# New checks: bypass-disclaimer, claude-version, daemon-reachable
# ---------------------------------------------------------------------------


def _make_fake_run_version(stdout: str = "2.1.150 (Claude Code)\n") -> object:
    """Return a subprocess.run replacement that succeeds for --version."""

    class _FakeCompleted:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted()

    return fake_run


class TestRunDoctor10Checks:
    """Tests for bypass-disclaimer, claude-version, and daemon-reachable checks."""

    def _monkeypatch_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        settings_content: str | None = None,
        roster_content: str | None = None,
        fake_run: object | None = None,
    ) -> tuple[Path, Path]:
        """Set up isolated path monkeypatches for new doctor checks.

        Returns (settings_path, roster_path) for further manipulation.
        """
        settings_path = tmp_path / ".claude" / "settings.json"
        roster_path = tmp_path / ".claude" / "daemon" / "roster.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        roster_path.parent.mkdir(parents=True, exist_ok=True)

        if settings_content is not None:
            settings_path.write_text(settings_content)
        if roster_content is not None:
            roster_path.write_text(roster_content)

        monkeypatch.setattr("cw.doctor._CLAUDE_SETTINGS_PATH", settings_path)
        monkeypatch.setattr("cw.doctor._ROSTER_PATH", roster_path)

        if fake_run is not None:
            monkeypatch.setattr("cw.doctor.subprocess.run", fake_run)
        else:
            monkeypatch.setattr(
                "cw.doctor.subprocess.run",
                _make_fake_run_version(),
            )
        return settings_path, roster_path

    def test_all_three_checks_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """run_doctor() includes bypass-disclaimer, claude-version, daemon-reachable."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        settings_path = tmp_config_dir / ".claude" / "settings.json"
        roster_path = tmp_config_dir / ".claude" / "daemon" / "roster.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        roster_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"skipDangerousModePermissionPrompt": True})
        )
        roster_path.write_text(json.dumps({"supervisorPid": 12345, "workers": {}}))

        monkeypatch.setattr("cw.doctor._CLAUDE_SETTINGS_PATH", settings_path)
        monkeypatch.setattr("cw.doctor._ROSTER_PATH", roster_path)
        monkeypatch.setattr("cw.doctor.subprocess.run", _make_fake_run_version())

        report = run_doctor()
        names = {c.name for c in report.checks}
        assert "bypass-disclaimer" in names
        assert "claude-version" in names
        assert "daemon-reachable" in names

    def test_bypass_disclaimer_warns_when_not_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """settings.json with {} → bypass-disclaimer is ok=True warn=True."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({}),
            roster_content=json.dumps({"supervisorPid": 12345}),
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "bypass-disclaimer")
        assert check.ok is True
        assert check.warn is True

    def test_bypass_disclaimer_ok_when_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """settings.json with flag set → bypass-disclaimer ok=True warn=False."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({"skipDangerousModePermissionPrompt": True}),
            roster_content=json.dumps({"supervisorPid": 12345}),
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "bypass-disclaimer")
        assert check.ok is True
        assert check.warn is False

    def test_daemon_reachable_warns_when_roster_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """No roster file → daemon-reachable is ok=True warn=True."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        # Provide settings so bypass-disclaimer doesn't interfere
        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({"skipDangerousModePermissionPrompt": True}),
            # no roster_content — file won't exist
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "daemon-reachable")
        assert check.ok is True
        assert check.warn is True

    def test_daemon_reachable_ok_when_supervisor_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """roster with supervisorPid: 12345 → daemon-reachable is ok=True warn=False."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({"skipDangerousModePermissionPrompt": True}),
            roster_content=json.dumps({"supervisorPid": 12345, "workers": {}}),
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "daemon-reachable")
        assert check.ok is True
        assert check.warn is False

    def test_daemon_reachable_warns_on_missing_supervisor_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """roster with workers but no supervisorPid → WARN, no KeyError."""
        monkeypatch.setenv("CW_BACKEND", "fake")
        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({"skipDangerousModePermissionPrompt": True}),
            roster_content=json.dumps({"workers": {}}),
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "daemon-reachable")
        assert check.ok is True
        assert check.warn is True

    def test_claude_version_missing_binary_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """FileNotFoundError from claude binary → claude-version is ok=False."""
        monkeypatch.setenv("CW_BACKEND", "fake")

        def fake_run_not_found(*_a: object, **_kw: object) -> object:
            msg = "no claude"
            raise FileNotFoundError(msg)

        self._monkeypatch_paths(
            monkeypatch,
            tmp_config_dir,
            settings_content=json.dumps({"skipDangerousModePermissionPrompt": True}),
            roster_content=json.dumps({"supervisorPid": 12345}),
            fake_run=fake_run_not_found,
        )
        report = run_doctor()
        check = next(c for c in report.checks if c.name == "claude-version")
        assert check.ok is False


def test_report_ok_unaffected_by_warn_checks() -> None:
    """WARN checks have ok=True; DoctorReport.ok stays True; cw doctor exits 0."""
    # Construct a DoctorReport with only WARN checks (ok=True, warn=True)
    warn_check = CheckResult("bypass-disclaimer", ok=True, warn=True, detail="not set")
    report = DoctorReport(
        version="0.0.0",
        backend=BackendName.FAKE,
        checks=[warn_check],
    )
    assert report.ok is True


# ---------------------------------------------------------------------------
# format_report footer tests
# ---------------------------------------------------------------------------


class TestFormatReportFooter:
    """format_report footer reflects ok/warn/fail state without contradicting."""

    def test_all_ok_no_warn_shows_healthy(self) -> None:
        """Only OK checks (no WARN, no FAIL) → 'status: healthy'."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[
                CheckResult("check-a", ok=True, warn=False, detail=""),
                CheckResult("check-b", ok=True, warn=False, detail=""),
            ],
        )
        rendered = format_report(report)
        assert rendered.endswith("status: healthy")

    def test_warn_checks_present_shows_advisory(self) -> None:
        """Any WARN check (ok=True, warn=True) → advisory footer, not plain healthy."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[
                CheckResult("bypass-disclaimer", ok=True, warn=True, detail="not set"),
                CheckResult("check-ok", ok=True, warn=False, detail=""),
            ],
        )
        rendered = format_report(report)
        # Must contain advisory wording — not plain 'healthy' by itself.
        assert "status: healthy — advisory warnings" in rendered
        # Exit-code contract: ok is still True, so this is NOT "problems detected".
        assert "problems detected" not in rendered

    def test_fail_check_shows_problems_detected(self) -> None:
        """Any FAIL check → 'status: problems detected'."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[
                CheckResult("check-fail", ok=False, warn=False, detail="broken"),
            ],
        )
        rendered = format_report(report)
        assert "status: problems detected" in rendered
        assert "healthy" not in rendered

    def test_clean_property_true_when_all_ok_no_warn(self) -> None:
        """DoctorReport.clean is True only when every check is ok and not warned."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[CheckResult("x", ok=True, warn=False, detail="")],
        )
        assert report.clean is True

    def test_clean_property_false_when_any_warn(self) -> None:
        """DoctorReport.clean is False when any check has warn=True."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[
                CheckResult("x", ok=True, warn=True, detail=""),
                CheckResult("y", ok=True, warn=False, detail=""),
            ],
        )
        assert report.clean is False

    def test_clean_property_false_when_any_fail(self) -> None:
        """DoctorReport.clean is False when any check has ok=False."""
        report = DoctorReport(
            version="0.0.0",
            backend=BackendName.FAKE,
            checks=[CheckResult("x", ok=False, warn=False, detail="")],
        )
        assert report.clean is False


# ---------------------------------------------------------------------------
# _check_claude_version: version-floor and returncode tests
# ---------------------------------------------------------------------------


class TestCheckClaudeVersion:
    """Direct tests for _check_claude_version via monkeypatched subprocess.run."""

    def _mk_proc(self, stdout: str = "", returncode: int = 0) -> object:
        class _Proc:
            pass

        p = _Proc()
        p.stdout = stdout  # type: ignore[attr-defined]
        p.stderr = ""  # type: ignore[attr-defined]
        p.returncode = returncode  # type: ignore[attr-defined]
        return p

    def test_version_above_floor_ok_no_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Version >= 2.1.139 → ok=True, warn=False."""
        from cw.doctor import _check_claude_version

        monkeypatch.setattr(
            "cw.doctor.subprocess.run",
            lambda *_a, **_kw: self._mk_proc("2.1.150 (Claude Code)\n"),
        )
        result = _check_claude_version()
        assert result.ok is True
        assert result.warn is False
        assert "2.1.150" in result.detail

    def test_version_equal_floor_ok_no_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Version == 2.1.139 → ok=True, warn=False (floor is inclusive)."""
        from cw.doctor import _check_claude_version

        monkeypatch.setattr(
            "cw.doctor.subprocess.run",
            lambda *_a, **_kw: self._mk_proc("2.1.139 (Claude Code)\n"),
        )
        result = _check_claude_version()
        assert result.ok is True
        assert result.warn is False

    def test_version_below_floor_ok_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Version < 2.1.139 → ok=True, warn=True, detail contains upgrade hint."""
        from cw.doctor import _check_claude_version

        monkeypatch.setattr(
            "cw.doctor.subprocess.run",
            lambda *_a, **_kw: self._mk_proc("2.0.0 (Claude Code)\n"),
        )
        result = _check_claude_version()
        assert result.ok is True
        assert result.warn is True
        assert "2.1.139" in result.detail

    def test_unparseable_version_ok_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-numeric version → ok=True, warn=True, detail mentions parse failure."""
        from cw.doctor import _check_claude_version

        monkeypatch.setattr(
            "cw.doctor.subprocess.run",
            lambda *_a, **_kw: self._mk_proc("not-a-version\n"),
        )
        result = _check_claude_version()
        assert result.ok is True
        assert result.warn is True
        assert "could not parse" in result.detail

    def test_nonzero_returncode_ok_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binary exits non-zero → ok=True, warn=True, detail includes returncode."""
        from cw.doctor import _check_claude_version

        monkeypatch.setattr(
            "cw.doctor.subprocess.run",
            lambda *_a, **_kw: self._mk_proc("some output\n", returncode=1),
        )
        result = _check_claude_version()
        assert result.ok is True
        assert result.warn is True
        assert "1" in result.detail  # returncode appears in detail
