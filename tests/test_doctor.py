"""Tests for cw.doctor — environment preflight checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw.cli import main
from cw.cmux import FakeCmuxAdapter
from cw.doctor import CheckResult, DoctorReport, format_report, run_doctor

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
    """run_doctor returns a healthy report on the fake backend."""

    def test_report_includes_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _stub_claude_version_ok(monkeypatch)
        report = run_doctor()
        assert report.version

    def test_report_ok_on_fake_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _stub_claude_version_ok(monkeypatch)
        report = run_doctor()
        assert report.ok


class TestFormatReport:
    def test_render_contains_ok_and_fail_markers(self) -> None:
        report = DoctorReport(
            version="0.0.0",
            checks=[CheckResult("check-fail", ok=False, detail="broken")],
        )
        rendered = format_report(report)
        assert "FAIL" in rendered
        assert "status: problems detected" in rendered

    def test_healthy_report_ends_with_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _stub_claude_version_ok(monkeypatch)
        report = run_doctor()
        rendered = format_report(report)
        assert "status: healthy" in rendered


class TestDoctorCli:
    def test_cli_exit_zero_on_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _stub_claude_version_ok(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "status: healthy" in result.output

    def test_cli_exit_nonzero_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        # Simulate a failing check by stubbing run_doctor to return a failing report.
        monkeypatch.setattr(
            "cw.cli.run_doctor",
            lambda **_kwargs: DoctorReport(
                version="0.0.0",
                checks=[CheckResult("check-fail", ok=False, detail="broken")],
            ),
        )
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
    from datetime import UTC
    from datetime import datetime as _dt

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
                    # Older than SPAWN_GRACE_SECONDS — phantom-eligible.
                    started_at=_dt(2026, 4, 19, tzinfo=UTC),
                ),
            ]
        )
    )

    # Non-empty live set bypasses reconcile's outage guard; "gone" still
    # isn't live so phantom is still reaped.
    monkeypatch.setattr(
        "cw.reconcile._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )

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

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--reap"])
    assert result.exit_code == 0, result.output
    assert "reconciliation" in result.output
    # Task may be reverted by the wedge reap (wedge/task-running-completed-session)
    # before reconcile runs, or by reconcile's revert_completed_silent_tasks sweep.
    # Either way the task must end up PENDING.
    reverted_by_wedge = "task-running-completed-session" in result.output
    reverted_by_reconcile = "reverted 1 ticket(s)" in result.output
    assert reverted_by_wedge or reverted_by_reconcile, result.output

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
            checks=[CheckResult("x", ok=True, warn=False, detail="")],
        )
        assert report.clean is True

    def test_clean_property_false_when_any_warn(self) -> None:
        """DoctorReport.clean is False when any check has warn=True."""
        report = DoctorReport(
            version="0.0.0",
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


# ---------------------------------------------------------------------------
# Direct tests for loader failure paths covered by narrowed BLE excepts
# (src/cw/doctor.py:124 _check_config_file, :163 _check_dev_queue)
# ---------------------------------------------------------------------------


class TestCheckConfigFileLoaderFailure:
    """_check_config_file returns ok=False when load_clients raises a narrowed type."""

    def test_yaml_error_from_load_clients_is_reported_as_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """yaml.YAMLError from load_clients → ok=False, parse-failure detail."""
        import yaml

        from cw.config import clients_file
        from cw.doctor import _check_config_file

        # File must exist so the try/except path runs (existence-true branch
        # short-circuits to ok=True before reaching the loader).
        clients_file().write_text("not: valid: yaml: here")

        yaml_msg = "malformed clients.yaml"

        def fake_load_clients() -> object:
            raise yaml.YAMLError(yaml_msg)

        monkeypatch.setattr("cw.doctor.load_clients", fake_load_clients)
        result = _check_config_file()
        assert result.ok is False
        assert "parse failed" in result.detail
        assert yaml_msg in result.detail


class TestCheckDevQueueLoaderFailure:
    """_check_dev_queue returns ok=False when load_dev_queue raises a narrowed type."""

    def test_json_decode_error_from_load_dev_queue_is_reported_as_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """JSONDecodeError from load_dev_queue → ok=False, load-failure detail."""
        from cw.doctor import _check_dev_queue

        json_msg = "corrupt dev_queue.json"

        def fake_load_dev_queue() -> object:
            raise json.JSONDecodeError(json_msg, "", 0)

        monkeypatch.setattr("cw.doctor.load_dev_queue", fake_load_dev_queue)
        result = _check_dev_queue()
        assert result.ok is False
        assert "load failed" in result.detail


class TestCheckWorkspacePaths:
    """Tests for _check_workspace_paths doctor check."""

    def test_missing_workspace_returns_fail_result(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Client with non-existent workspace_path returns ok=False."""
        from cw.doctor import _check_workspace_paths

        missing_dir = tmp_path / "nonexistent"
        # Write clients.yaml with missing path
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  bad-client:\n    workspace_path: {missing_dir}\n"
            f"    default_branch: main\n"
        )

        results = _check_workspace_paths()

        assert len(results) == 1
        assert results[0].ok is False
        assert "bad-client" in results[0].name
        assert "does not exist" in results[0].detail

    def test_existing_workspace_returns_no_results(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Client with existing workspace_path returns no results (all ok)."""
        from cw.doctor import _check_workspace_paths

        existing_dir = tmp_path / "real-ws"
        existing_dir.mkdir()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  ok-client:\n    workspace_path: {existing_dir}\n"
            f"    default_branch: main\n"
        )

        results = _check_workspace_paths()

        assert results == []

    def test_missing_clients_yaml_returns_empty(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """No clients.yaml → _check_workspace_paths returns [] (no crash)."""
        from cw.doctor import _check_workspace_paths

        results = _check_workspace_paths()
        assert results == []

    def test_load_clients_exception_returns_empty(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """load_clients() raising any exception returns [] (no crash)."""
        from cw.doctor import _check_workspace_paths

        def boom() -> object:
            msg = "unexpected parse error"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.doctor.load_clients", boom)
        results = _check_workspace_paths()
        assert results == []

    def test_run_doctor_includes_workspace_check(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_doctor surfaces workspace path failures in the report."""
        _stub_claude_version_ok(monkeypatch)
        missing_dir = tmp_path / "nonexistent"
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            f"clients:\n  bad-client:\n    workspace_path: {missing_dir}\n"
            f"    default_branch: main\n"
        )

        report = run_doctor()

        workspace_checks = [c for c in report.checks if c.name.startswith("workspace/")]
        assert len(workspace_checks) == 1
        assert workspace_checks[0].ok is False


# ---------------------------------------------------------------------------
# Wedge detection tests
# ---------------------------------------------------------------------------


class TestWedgeFindingDataclass:
    """WedgeFinding dataclass structure and DoctorReport integration."""

    def test_fields_exist(self) -> None:
        from cw.doctor import WedgeFinding

        wf = WedgeFinding(
            wedge_class="wedge/pane-idle-but-active",
            session_id="abc",
            ticket_id="123",
            recipe="run cw doctor --reap",
            state_file="/tmp/x.json",
        )
        assert wf.wedge_class == "wedge/pane-idle-but-active"
        assert wf.session_id == "abc"
        assert wf.ticket_id == "123"
        assert wf.recipe == "run cw doctor --reap"
        assert wf.state_file == "/tmp/x.json"

    def test_doctor_report_has_wedge_findings(self) -> None:
        from cw.doctor import DoctorReport

        report = DoctorReport(version="0.0.0", checks=[])
        assert report.wedge_findings == []

    def test_wedge_findings_do_not_affect_ok(self) -> None:
        from cw.doctor import DoctorReport, WedgeFinding

        wf = WedgeFinding(
            wedge_class="wedge/pane-idle-but-active",
            session_id="abc",
            ticket_id="123",
            recipe="fix it",
            state_file="/tmp/x.json",
        )
        report = DoctorReport(
            version="0.0.0",
            checks=[CheckResult("check-a", ok=True, detail="")],
            wedge_findings=[wf],
        )
        assert report.ok is True


class TestWedgePaneIdleButActive:
    """wedge/pane-idle-but-active detection logic."""

    def _make_active_session(
        self, tmp_path: Path, *, surface_ref: str | None = "s:0.1"
    ) -> object:
        from datetime import UTC, datetime

        from cw.models import Session, SessionPurpose, SessionStatus

        wt = tmp_path / "worktree"
        wt.mkdir(parents=True, exist_ok=True)
        return Session(
            id="sess-active",
            name="client-a/auto-dev/TST-1",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=tmp_path,
            worktree_path=wt if surface_ref is not None else None,
            surface_ref=surface_ref,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_detected_when_shell_and_old_mtime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        import time

        from cw.cmux import FakeCmuxAdapter
        from cw.config import save_state
        from cw.dev_queue import save_dev_queue
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        adapter = FakeCmuxAdapter()
        old_time = time.time() - 700
        adapter.set_pane_info("s:0.1", {"cmd": "bash", "last_activity": None})

        # Create a file with old mtime
        test_file = tmp_path / "worktree" / "test.py"
        test_file.write_text("code")
        import os

        os.utime(str(test_file), (old_time, old_time))

        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        assert len(findings) == 1
        assert findings[0].wedge_class == "wedge/pane-idle-but-active"
        assert findings[0].session_id == "sess-active"

    def test_not_detected_nonshell_cmd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        import time

        from cw.cmux import FakeCmuxAdapter
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]

        adapter = FakeCmuxAdapter()
        adapter.set_pane_info("s:0.1", {"cmd": "claude", "last_activity": None})

        old_time = time.time() - 700
        test_file = tmp_path / "worktree" / "test.py"
        test_file.write_text("code")
        import os

        os.utime(str(test_file), (old_time, old_time))

        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        assert findings == []

    def test_not_detected_recent_mtime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.cmux import FakeCmuxAdapter
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]

        adapter = FakeCmuxAdapter()
        adapter.set_pane_info("s:0.1", {"cmd": "bash", "last_activity": None})

        # File with recent mtime (now)
        test_file = tmp_path / "worktree" / "test.py"
        test_file.write_text("fresh code")

        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        assert findings == []

    def test_not_detected_no_surface_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.cmux import FakeCmuxAdapter
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path, surface_ref=None)
        state = CwState(sessions=[session])  # type: ignore[list-item]

        adapter = FakeCmuxAdapter()
        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        assert findings == []

    def test_git_dir_excluded_from_mtime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        import os
        import time

        from cw.cmux import FakeCmuxAdapter
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]

        adapter = FakeCmuxAdapter()
        adapter.set_pane_info("s:0.1", {"cmd": "bash", "last_activity": None})

        wt = tmp_path / "worktree"
        old_time = time.time() - 700

        # Create old non-.git file
        old_file = wt / "old.py"
        old_file.write_text("old code")
        os.utime(str(old_file), (old_time, old_time))

        # Create recent .git/FETCH_HEAD — must be excluded
        git_dir = wt / ".git"
        git_dir.mkdir()
        fetch_head = git_dir / "FETCH_HEAD"
        fetch_head.write_text("ref")
        # leave fetch_head with current mtime (recent)

        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        # .git/ excluded → old.py is only non-.git file → finding IS emitted
        assert len(findings) == 1
        assert findings[0].wedge_class == "wedge/pane-idle-but-active"

    def test_inspect_pane_empty_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.cmux import FakeCmuxAdapter
        from cw.doctor import _check_wedge_pane_idle
        from cw.models import CwState, DevQueueStore

        session = self._make_active_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]

        adapter = FakeCmuxAdapter()
        # inspect_pane returns {} (default) — fail-open, skip

        queue = DevQueueStore(tasks=[])
        findings = _check_wedge_pane_idle(state, queue, adapter)
        assert findings == []


class TestWedgeTaskRunningNoSession:
    """wedge/task-running-no-session detection logic."""

    def test_orphan_detected_null_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from cw.config import save_state
        from cw.doctor import _check_wedge_task_running_no_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        # Task created long ago with session_id=None
        old_time = datetime.now(UTC) - timedelta(seconds=120)
        task = TicketTask(
            ticket_id="TST-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
            created_at=old_time,
        )
        state = CwState(sessions=[])
        save_state(state)
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_no_session(state, queue)
        assert len(findings) == 1
        assert findings[0].wedge_class == "wedge/task-running-no-session"

    def test_grace_window_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from cw.doctor import _check_wedge_task_running_no_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        # Task created 5 seconds ago
        recent_time = datetime.now(UTC) - timedelta(seconds=5)
        task = TicketTask(
            ticket_id="TST-2",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
            created_at=recent_time,
        )
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_no_session(state, queue)
        assert findings == []

    def test_active_session_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from cw.doctor import _check_wedge_task_running_no_session
        from cw.models import (
            CwState,
            DevQueueStore,
            QueueItemStatus,
            Session,
            SessionPurpose,
            SessionStatus,
            TicketTask,
        )

        session = Session(
            id="live-sess",
            name="client-a/auto-dev/TST-3",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp"),
        )
        old_time = datetime.now(UTC) - timedelta(seconds=120)
        task = TicketTask(
            ticket_id="TST-3",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="live-sess",
            created_at=old_time,
        )
        state = CwState(sessions=[session])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_no_session(state, queue)
        assert findings == []

    def test_correct_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from cw.doctor import _check_wedge_task_running_no_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        old_time = datetime.now(UTC) - timedelta(seconds=120)
        task = TicketTask(
            ticket_id="TST-99",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
            created_at=old_time,
        )
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_no_session(state, queue)
        assert len(findings) == 1
        f = findings[0]
        assert f.ticket_id == "TST-99"
        assert "PENDING" in f.recipe or "revert" in f.recipe.lower()


class TestWedgeTaskRunningCompletedSession:
    """wedge/task-running-completed-session detection logic."""

    def _make_completed_session(self, tmp_path: Path, sid: str = "comp-sess") -> object:
        from datetime import UTC, datetime

        from cw.models import CompletionReason, Session, SessionPurpose, SessionStatus

        return Session(
            id=sid,
            name="client-a/auto-dev/TST-C1",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.COMPLETED,
            workspace_path=tmp_path,
            completed_reason=CompletionReason.NORMAL,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_detected_completed_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_task_running_completed_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        session = self._make_completed_session(tmp_path)
        task = TicketTask(
            ticket_id="TST-C1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="comp-sess",
        )
        state = CwState(sessions=[session])  # type: ignore[list-item]
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_completed_session(state, queue)
        assert len(findings) == 1
        assert findings[0].wedge_class == "wedge/task-running-completed-session"

    def test_not_detected_active_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.doctor import _check_wedge_task_running_completed_session
        from cw.models import (
            CwState,
            DevQueueStore,
            QueueItemStatus,
            Session,
            SessionPurpose,
            SessionStatus,
            TicketTask,
        )

        session = Session(
            id="active-sess",
            name="client-a/auto-dev/TST-C2",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=tmp_path,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        task = TicketTask(
            ticket_id="TST-C2",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="active-sess",
        )
        state = CwState(sessions=[session])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_completed_session(state, queue)
        assert findings == []

    def test_not_detected_null_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_task_running_completed_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="TST-C3",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
        )
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_completed_session(state, queue)
        assert findings == []

    def test_correct_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_task_running_completed_session
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        session = self._make_completed_session(tmp_path, sid="comp-sess-field")
        task = TicketTask(
            ticket_id="TST-CF",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="comp-sess-field",
        )
        state = CwState(sessions=[session])  # type: ignore[list-item]
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_task_running_completed_session(state, queue)
        assert len(findings) == 1
        f = findings[0]
        assert "PENDING" in f.recipe


class TestWedgeRepoAheadOfQueue:
    """wedge/repo-ahead-of-queue detection logic."""

    def _make_running_task(
        self,
        tmp_path: Path,
        ticket_id: str = "TST-R1",
        session_id: str | None = None,
    ) -> object:
        from cw.models import QueueItemStatus, TicketTask

        return TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            worktree_path=tmp_path,
        )

    def test_ahead_no_pr_emits_warn_recipe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:

        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import CwState, DevQueueStore

        task = self._make_running_task(tmp_path, ticket_id="TST-R1")
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])  # type: ignore[list-item]

        call_count = [0]

        class _Proc:
            def __init__(self, rc: int, out: str) -> None:
                self.returncode = rc
                self.stdout = out

        def fake_run(args: list[str], **_kw: object) -> _Proc:
            call_count[0] += 1
            if "remote" in args and "get-url" in args:
                return _Proc(0, "https://github.com/org/repo.git\n")
            if "ls-remote" in args:
                return _Proc(0, "abc123\trefs/heads/auto-dev/TST-R1\n")
            if "pr" in args and "list" in args:
                return _Proc(0, "[]\n")
            return _Proc(0, "")

        monkeypatch.setattr("cw.doctor._sp.run", fake_run)
        findings = _check_wedge_repo_ahead(state, queue)
        assert len(findings) == 1
        f = findings[0]
        assert f.wedge_class == "wedge/repo-ahead-of-queue"
        assert "no open PR" in f.recipe or "cw spawn-complete" in f.recipe

    def test_ahead_open_pr_emits_shipped_recipe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        import json

        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import CwState, DevQueueStore

        task = self._make_running_task(tmp_path, ticket_id="TST-R2")
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])  # type: ignore[list-item]

        class _Proc:
            def __init__(self, rc: int, out: str) -> None:
                self.returncode = rc
                self.stdout = out

        def fake_run(args: list[str], **_kw: object) -> _Proc:
            if "remote" in args and "get-url" in args:
                return _Proc(0, "https://github.com/org/repo.git\n")
            if "ls-remote" in args:
                return _Proc(0, "abc123\trefs/heads/auto-dev/TST-R2\n")
            if "pr" in args and "list" in args:
                return _Proc(0, json.dumps([{"state": "OPEN"}]))
            return _Proc(0, "")

        monkeypatch.setattr("cw.doctor._sp.run", fake_run)
        findings = _check_wedge_repo_ahead(state, queue)
        assert len(findings) == 1
        assert "cw spawn-complete" in findings[0].recipe

    def test_not_ahead_no_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import CwState, DevQueueStore

        task = self._make_running_task(tmp_path, ticket_id="TST-R3")
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])  # type: ignore[list-item]

        class _Proc:
            def __init__(self, rc: int, out: str) -> None:
                self.returncode = rc
                self.stdout = out

        def fake_run(args: list[str], **_kw: object) -> _Proc:
            if "remote" in args and "get-url" in args:
                return _Proc(0, "https://github.com/org/repo.git\n")
            if "ls-remote" in args:
                return _Proc(0, "")  # empty — not ahead
            return _Proc(0, "")

        monkeypatch.setattr("cw.doctor._sp.run", fake_run)
        findings = _check_wedge_repo_ahead(state, queue)
        assert findings == []

    def test_ls_remote_fail_no_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import CwState, DevQueueStore

        task = self._make_running_task(tmp_path, ticket_id="TST-R4")
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])  # type: ignore[list-item]

        class _Proc:
            def __init__(self, rc: int, out: str) -> None:
                self.returncode = rc
                self.stdout = out

        def fake_run(args: list[str], **_kw: object) -> _Proc:
            if "remote" in args and "get-url" in args:
                return _Proc(0, "https://github.com/org/repo.git\n")
            if "ls-remote" in args:
                return _Proc(1, "")  # failure
            return _Proc(0, "")

        monkeypatch.setattr("cw.doctor._sp.run", fake_run)
        findings = _check_wedge_repo_ahead(state, queue)
        assert findings == []

    def test_worktree_path_none_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        task = TicketTask(
            ticket_id="TST-R5",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            worktree_path=None,
        )
        state = CwState(sessions=[])
        queue = DevQueueStore(tasks=[task])
        findings = _check_wedge_repo_ahead(state, queue)
        assert findings == []

    def test_branch_resolution_uses_session_branch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        from datetime import UTC, datetime

        from cw.doctor import _check_wedge_repo_ahead
        from cw.models import (
            CwState,
            DevQueueStore,
            QueueItemStatus,
            Session,
            SessionPurpose,
            SessionStatus,
            TicketTask,
        )

        session = Session(
            id="sess-branch",
            name="client-a/auto-dev/TST-R6",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=tmp_path,
            branch="my-branch",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        task = TicketTask(
            ticket_id="TST-R6",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="sess-branch",
            worktree_path=tmp_path,
        )
        state = CwState(sessions=[session])
        queue = DevQueueStore(tasks=[task])

        ls_remote_args_seen: list[list[str]] = []

        class _Proc:
            def __init__(self, rc: int, out: str) -> None:
                self.returncode = rc
                self.stdout = out

        def fake_run(args: list[str], **_kw: object) -> _Proc:
            if "remote" in args and "get-url" in args:
                return _Proc(0, "https://github.com/org/repo.git\n")
            if "ls-remote" in args:
                ls_remote_args_seen.append(list(args))
                return _Proc(0, "abc123\trefs/heads/my-branch\n")
            if "pr" in args and "list" in args:
                return _Proc(0, "[]\n")
            return _Proc(0, "")

        monkeypatch.setattr("cw.doctor._sp.run", fake_run)
        _check_wedge_repo_ahead(state, queue)
        # The ls-remote call must reference my-branch, not auto-dev/TST-R6
        assert any("my-branch" in str(a) for a in ls_remote_args_seen)


class TestWedgeReapRecipes:
    """_reap_wedge_findings applies correct mutations per wedge class."""

    def _make_session(self, tmp_path: Path, sid: str = "reap-sess") -> object:
        from datetime import UTC, datetime

        from cw.models import Session, SessionPurpose, SessionStatus

        return Session(
            id=sid,
            name="client-a/auto-dev/TST-REAP",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=tmp_path,
            surface_ref="s:0.1",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_class1_reap_mutates_session_and_queue(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        from cw.config import load_state, save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.doctor import WedgeFinding, _reap_wedge_findings
        from cw.models import (
            CwState,
            DevQueueStore,
            QueueItemStatus,
            SessionStatus,
            TicketTask,
        )

        session = self._make_session(tmp_path)
        state = CwState(sessions=[session])  # type: ignore[list-item]
        save_state(state)

        task = TicketTask(
            ticket_id="TST-REAP",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reap-sess",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.config import state_file as _sf

        finding = WedgeFinding(
            wedge_class="wedge/pane-idle-but-active",
            session_id="reap-sess",
            ticket_id="TST-REAP",
            recipe="fix",
            state_file=str(_sf()),
        )

        _reap_wedge_findings([finding], state, mock_cmux_adapter)

        # session should be COMPLETED
        reloaded = load_state()
        sess = next(s for s in reloaded.sessions if s.id == "reap-sess")
        assert sess.status == SessionStatus.COMPLETED

        # queue task should be PENDING
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TST-REAP")
        assert t.status == QueueItemStatus.PENDING

        # adapter.close should have been called
        assert len(mock_cmux_adapter.calls["close"]) == 1

    def test_class2_reap_reverts_queue_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        from cw.config import save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.doctor import WedgeFinding, _reap_wedge_findings
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        state = CwState(sessions=[])
        save_state(state)

        task = TicketTask(
            ticket_id="TST-C2",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.config import state_file as _sf

        finding = WedgeFinding(
            wedge_class="wedge/task-running-no-session",
            session_id=None,
            ticket_id="TST-C2",
            recipe="fix",
            state_file=str(_sf()),
        )

        _reap_wedge_findings([finding], state, mock_cmux_adapter)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TST-C2")
        assert t.status == QueueItemStatus.PENDING

    def test_class3_reap_reverts_queue_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        from cw.config import save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.doctor import WedgeFinding, _reap_wedge_findings
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        state = CwState(sessions=[])
        save_state(state)

        task = TicketTask(
            ticket_id="TST-C3",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="some-completed-sess",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.config import state_file as _sf

        finding = WedgeFinding(
            wedge_class="wedge/task-running-completed-session",
            session_id="some-completed-sess",
            ticket_id="TST-C3",
            recipe="fix",
            state_file=str(_sf()),
        )

        _reap_wedge_findings([finding], state, mock_cmux_adapter)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TST-C3")
        assert t.status == QueueItemStatus.PENDING

    def test_class4_no_reap_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        from cw.config import save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.doctor import WedgeFinding, _reap_wedge_findings
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        state = CwState(sessions=[])
        save_state(state)

        task = TicketTask(
            ticket_id="TST-C4",
            client="client-a",
            status=QueueItemStatus.RUNNING,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.config import state_file as _sf

        finding = WedgeFinding(
            wedge_class="wedge/repo-ahead-of-queue",
            session_id=None,
            ticket_id="TST-C4",
            recipe="advisory",
            state_file=str(_sf()),
        )

        _reap_wedge_findings([finding], state, mock_cmux_adapter)

        # advisory only — queue should remain RUNNING
        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TST-C4")
        assert t.status == QueueItemStatus.RUNNING

    def test_adapter_close_only_for_class1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        from cw.config import save_state
        from cw.dev_queue import save_dev_queue
        from cw.doctor import WedgeFinding, _reap_wedge_findings
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        state = CwState(sessions=[])
        save_state(state)

        task = TicketTask(
            ticket_id="TST-NCLOSE",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=None,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        from cw.config import state_file as _sf

        finding = WedgeFinding(
            wedge_class="wedge/task-running-no-session",
            session_id=None,
            ticket_id="TST-NCLOSE",
            recipe="fix",
            state_file=str(_sf()),
        )

        _reap_wedge_findings([finding], state, mock_cmux_adapter)

        # adapter.close must NOT have been called for class-2
        assert len(mock_cmux_adapter.calls["close"]) == 0

    def test_reap_false_no_mutations(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_config_dir: Path,
        mock_cmux_adapter: FakeCmuxAdapter,
    ) -> None:
        """run_doctor(reap=False) must not trigger _reap_wedge_findings."""
        from cw.config import save_state
        from cw.dev_queue import load_dev_queue, save_dev_queue
        from cw.models import CwState, DevQueueStore, QueueItemStatus, TicketTask

        session = self._make_session(tmp_path, sid="no-reap-sess")
        state = CwState(sessions=[session])  # type: ignore[list-item]
        save_state(state)

        task = TicketTask(
            ticket_id="TST-NOREAP",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="no-reap-sess",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Monkeypatch to prevent wedge check from modifying the report
        monkeypatch.setattr(
            "cw.doctor._check_wedge_pane_idle",
            lambda *_a, **_kw: [],
        )
        monkeypatch.setattr(
            "cw.doctor._check_wedge_task_running_no_session",
            lambda *_a, **_kw: [],
        )
        monkeypatch.setattr(
            "cw.doctor._check_wedge_task_running_completed_session",
            lambda *_a, **_kw: [],
        )
        monkeypatch.setattr(
            "cw.doctor._check_wedge_repo_ahead",
            lambda *_a, **_kw: [],
        )

        # run_doctor without --reap, queue should remain RUNNING
        from cw.doctor import run_doctor

        _stub_claude_version_ok(monkeypatch)
        run_doctor(reap=False)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "TST-NOREAP")
        assert t.status == QueueItemStatus.RUNNING


class TestDoctorJsonMode:
    """cw doctor --json produces parseable JSON with required structure."""

    def test_output_is_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        import json

        from click.testing import CliRunner

        from cw.cli import main

        _stub_claude_version_ok(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        json.loads(result.output)  # must not raise

    def test_json_has_required_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        import json

        from click.testing import CliRunner

        from cw.cli import main

        _stub_claude_version_ok(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json"])
        data = json.loads(result.output)
        assert "version" in data
        assert "ok" in data
        assert "checks" in data
        assert "wedge_findings" in data

    def test_json_wedge_findings_structure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        import json

        from click.testing import CliRunner

        from cw.cli import main
        from cw.doctor import WedgeFinding

        _stub_claude_version_ok(monkeypatch)

        wf = WedgeFinding(
            wedge_class="wedge/pane-idle-but-active",
            session_id="abc",
            ticket_id="123",
            recipe="fix it",
            state_file="/tmp/x.json",
        )
        monkeypatch.setattr(
            "cw.cli.run_doctor",
            lambda **_kw: __import__(
                "cw.doctor", fromlist=["DoctorReport"]
            ).DoctorReport(
                version="0.0.0",
                checks=[CheckResult("check-a", ok=True, detail="")],
                wedge_findings=[wf],
            ),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json"])
        data = json.loads(result.output)
        assert len(data["wedge_findings"]) == 1
        wfd = data["wedge_findings"][0]
        assert "wedge_class" in wfd
        assert "session_id" in wfd
        assert "ticket_id" in wfd
        assert "recipe" in wfd
        assert "state_file" in wfd

    def test_json_composes_with_reap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        import json

        from click.testing import CliRunner

        from cw.cli import main

        _stub_claude_version_ok(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json", "--reap"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "ok" in data

    def test_exit_0_when_healthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        _stub_claude_version_ok(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 0

    def test_exit_1_when_checks_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        monkeypatch.setattr(
            "cw.cli.run_doctor",
            lambda **_kw: __import__(
                "cw.doctor", fromlist=["DoctorReport"]
            ).DoctorReport(
                version="0.0.0",
                checks=[CheckResult("check-fail", ok=False, detail="broken")],
            ),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 1


class TestWedgeRegression:
    """Regression tests: existing checks unaffected, excluded classes absent."""

    def test_existing_linkage_checks_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        from cw.config import save_state
        from cw.doctor import run_doctor
        from cw.models import CwState, Session, SessionPurpose, SessionStatus

        _stub_claude_version_ok(monkeypatch)
        # Session with dangling worker — linkage/dangling-worker should fail
        session = Session(
            id="orch-reg",
            name="client/impl",
            client="client",
            purpose=SessionPurpose.IMPL,
            status=SessionStatus.ACTIVE,
            workspace_path=tmp_path,
            worker_session_ids=["missing-worker-reg"],
        )
        save_state(CwState(sessions=[session]))
        report = run_doctor()
        dw = next(c for c in report.checks if c.name == "linkage/dangling-worker")
        assert not dw.ok
        # wedge_findings should be empty (no queue tasks RUNNING)
        assert report.wedge_findings == []

    def test_classes_5_6_not_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        from cw.doctor import run_doctor

        _stub_claude_version_ok(monkeypatch)
        report = run_doctor()
        excluded = {
            "wedge/supervisor-says-done-cw-says-running",
            "wedge/cw-says-done-supervisor-says-alive",
        }
        for wf in report.wedge_findings:
            assert wf.wedge_class not in excluded
