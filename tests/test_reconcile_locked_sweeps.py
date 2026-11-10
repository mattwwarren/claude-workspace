"""Unit tests for cw.reconcile.locked_sweeps — the in-lock sweep wiring.

Moved out of ``tests/test_reconcile_core.py`` with the #2896 split. Each
sweep is patched on ``cw.reconcile.locked_sweeps``, the module that calls it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cw.config import (
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    _verify_supervisor_session_id,
    locked_sweeps,
    reconcile,
)
from cw.reconcile import core as reconcile_core
from cw.reconcile._shared import new_scope_probes
from cw.reconcile.codex_boot import CleanProbes
from cw.reconcile.deferred import DeferredReconcileJobs
from cw.reconcile.dirty_checks import DirtyChecks
from cw.reconcile.harvest_synthesis import HarvestFacts
from tests._clients_yaml import write_clients_yaml
from tests._dirty_check_helpers import fresh_roster_snapshot
from tests._reconcile_helpers import (
    _auto_config,
    _mk_session,
)
from tests.conftest import CapturedEvent, _make_daemon_session, _make_ticket_task


class TestVerifySupervisorSessionId:
    """_verify_supervisor_session_id compares stored csid against supervisor state."""

    def _mk_daemon_session(
        self,
        sid: str,
        surface_ref: str | None,
        claude_session_id: str | None,
        status: SessionStatus = SessionStatus.ACTIVE,
    ) -> Session:
        return _make_daemon_session(
            id=sid,
            name=f"client-a/auto-dev/{sid}",
            status=status,
            worktree_path=None,
            surface_ref=surface_ref,
            claude_session_id=claude_session_id,
            started_at=datetime(2026, 4, 19, tzinfo=UTC),
        )

    def _write_supervisor_state(
        self, jobs_path: Path, short_id: str, resume_id: str
    ) -> None:
        job_dir = jobs_path / short_id
        job_dir.mkdir(parents=True)
        (job_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": resume_id}),
            encoding="utf-8",
        )

    def test_matching_id_is_noop(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resumeSessionId matches claude_session_id — no mutation, no event."""
        short_id = "a1b2c3d4"
        full_uuid = "a1b2c3d4-0000-0000-0000-000000000001"
        jobs_path = tmp_config_dir / "jobs"
        self._write_supervisor_state(jobs_path, short_id, full_uuid)

        session = self._mk_daemon_session("s1", short_id, full_uuid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: full_uuid if sid == short_id else None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert load_state().sessions[0].surface_ref == short_id

    def test_mismatch_clears_claude_session_id(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mismatch: claude_session_id cleared, surface_ref left intact."""
        short_id = "b2c3d4e5"
        stored_csid = "b2c3d4e5-0000-0000-0000-000000000001"
        supervisor_resume_id = "ffffffff-dead-beef-dead-beefdeadbeef"

        session = self._mk_daemon_session("s2", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: supervisor_resume_id if sid == short_id else None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 1
        updated = load_state().sessions[0]
        assert updated.claude_session_id is None
        assert updated.surface_ref == short_id

    def test_mismatch_logs_warning(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """On mismatch, a warning containing 'csid_mismatch' is logged."""

        short_id = "c3d4e5f6"
        stored_csid = "c3d4e5f6-0000-0000-0000-000000000001"
        supervisor_resume_id = "ffffffff-dead-beef-dead-beefdeadbeef"

        session = self._mk_daemon_session("s3", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: supervisor_resume_id if sid == short_id else None,
        )
        with caplog.at_level(logging.WARNING):
            _verify_supervisor_session_id(load_state())

        assert any("csid_mismatch" in rec.message for rec in caplog.records)

    def test_missing_state_json_is_noop(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No state.json → no continuity claim → no mutation."""
        short_id = "d4e5f6a7"
        stored_csid = "d4e5f6a7-0000-0000-0000-000000000001"

        session = self._mk_daemon_session("s4", short_id, stored_csid)
        state = CwState(sessions=[session])
        save_state(state)

        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda _sid, **_kw: None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert load_state().sessions[0].surface_ref == short_id

    def test_no_claude_session_id_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session without claude_session_id is skipped — nothing to compare."""
        short_id = "e5f6a7b8"
        session = self._mk_daemon_session("s5", short_id, None)
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_no_surface_ref_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session without surface_ref has nothing to look up — skipped."""
        full_uuid = "f6a7b8c9-0000-0000-0000-000000000001"
        session = self._mk_daemon_session("s6", None, full_uuid)
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_completed_session_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-live (COMPLETED) sessions are not checked."""
        short_id = "a7b8c9d0"
        stored_csid = "a7b8c9d0-0000-0000-0000-000000000001"
        session = self._mk_daemon_session(
            "s7", short_id, stored_csid, status=SessionStatus.COMPLETED
        )
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []

    def test_user_origin_session_is_skipped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-DAEMON (USER) sessions are not checked."""
        short_id = "b8c9d0e1"
        stored_csid = "b8c9d0e1-0000-0000-0000-000000000001"
        session = Session(
            id="s8",
            name="client-a/auto-dev/s8",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.USER,
            status=SessionStatus.ACTIVE,
            workspace_path=ClientConfig(
                name="client-a", workspace_path=Path("/tmp/ws")
            ).workspace_path,
            surface_ref=short_id,
            claude_session_id=stored_csid,
            started_at=datetime(2026, 4, 19, tzinfo=UTC),
        )
        state = CwState(sessions=[session])
        save_state(state)

        called: list[str] = []
        monkeypatch.setattr(
            "cw.reconcile._deps.read_supervisor_resume_session_id",
            lambda sid, **_kw: called.append(sid) or None,
        )
        cleared = _verify_supervisor_session_id(load_state())
        assert cleared == 0
        assert called == []


class TestConciergeAndEscalationWiring:
    """RFC 0008 capstone (#1015): wiring-only — both new sweeps run exactly
    once per reconcile() tick, in both the no-phantoms and phantom branches
    of _reconcile_locked."""

    def test_no_phantoms_branch_calls_both_sweeps_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        concierge_mock = MagicMock(return_value=[])
        escalation_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_concierge_recoveries", concierge_mock
        )
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_escalation_sweep", escalation_mock
        )

        reconcile()

        concierge_mock.assert_called_once()
        escalation_mock.assert_called_once()

    def test_phantom_branch_calls_both_sweeps_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A phantom (missing-surface) DAEMON session routes through the
        phantom-handling tail of _reconcile_locked — the sweeps must still
        fire exactly once there too."""
        state = CwState(sessions=[_mk_session("phantom-1", "missing-ref")])
        save_state(state)
        # A non-empty (but unrelated) roster keeps `native_live` non-empty so
        # _looks_like_daemon_outage's "roster looks totally dead" guard
        # doesn't short-circuit the tick before reaching either sweep.
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "unrelated1"}],
        )
        concierge_mock = MagicMock(return_value=[])
        escalation_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_concierge_recoveries", concierge_mock
        )
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_escalation_sweep", escalation_mock
        )

        report = reconcile()

        assert report.phantom_session_ids == ["phantom-1"]
        concierge_mock.assert_called_once()
        escalation_mock.assert_called_once()


class TestUnownedRunningSweepWiring:
    """#2591: the unbound-row adoption sweep runs in both terminal tails,
    before the TIMED_OUT backstop, and not on the daemon-outage early return."""

    @staticmethod
    def _record_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        order: list[str] = []
        real_adopt = locked_sweeps.run_unowned_running_recovery
        real_revert = locked_sweeps.revert_timed_out_tasks

        def _adopt(*, clients: dict[str, ClientConfig]) -> list[str]:
            order.append("unowned_running")
            return real_adopt(clients=clients)

        def _revert(dirty_checks: DirtyChecks | None = None) -> list[str]:
            order.append("revert_timed_out")
            return real_revert(dirty_checks)

        monkeypatch.setattr(locked_sweeps, "run_unowned_running_recovery", _adopt)
        monkeypatch.setattr(locked_sweeps, "revert_timed_out_tasks", _revert)
        return order

    def test_runs_before_the_timed_out_backstop_without_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._record_order(monkeypatch)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        report = reconcile()

        assert report.phantom_session_ids == []
        assert order == ["unowned_running", "revert_timed_out"]

    def test_runs_before_the_timed_out_backstop_with_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
        save_state(CwState(sessions=[_mk_session("s1", "missing-ref")]))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        order = self._record_order(monkeypatch)

        report = reconcile()

        assert report.phantom_session_ids == ["s1"]
        assert order == ["unowned_running", "revert_timed_out"]

    def test_skipped_on_the_daemon_outage_early_return(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[_mk_session("s1", "live-ref")]))
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        order = self._record_order(monkeypatch)

        reconcile()

        assert order == []

    def test_phantom_sweep_reverts_an_unbound_phantom_row_first(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        """A row whose claim session is a phantom is the phantom sweep's (keyed
        by ticket id); adoption then finds no RUNNING row and does nothing."""
        from cw.worktree import worktree_path_for
        from tests.conftest import _write_hook_context_file

        monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
        write_clients_yaml(client)
        wt = worktree_path_for(client, f"{client.feature_branch_prefix}/TKT-1")
        wt.mkdir(parents=True)
        _write_hook_context_file(
            wt,
            session_id="sess-daemon",
            ticket_id="TKT-1",
            client="client-a",
            task=_make_ticket_task(ticket_id="TKT-1", client="client-a", attempts=1),
        )
        sess = _mk_session("sess-daemon", "dead-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/TKT-1"
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="TKT-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        attempts=1,
                    )
                ]
            )
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        adopted = capture_events(
            "cw.reconcile.unowned_running",
            OrchestratorEventType.TASK_SESSION_ADOPTED,
        )

        report = reconcile()

        assert "TKT-1" in report.reverted_ticket_ids
        row = load_dev_queue().tasks[0]
        assert row.status == QueueItemStatus.PENDING
        assert row.session_id is None
        assert adopted == []


def _spy_core(
    monkeypatch: pytest.MonkeyPatch, order: list[str], name: str, label: str
) -> None:
    """Wrap ``cw.reconcile.locked_sweeps.<name>`` so each call appends *label*."""
    real = getattr(locked_sweeps, name)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        order.append(label)
        return real(*args, **kwargs)

    monkeypatch.setattr(locked_sweeps, name, _wrapped)


class TestSlotReleaseSweepWiring:
    """#2715: the lost-session sweep runs in both terminal tails after the
    backstops; the shipped release runs once, before the #2524 page sweep;
    neither runs on the daemon-outage early return."""

    _TAIL = (
        ("run_unowned_running_recovery", "unowned_running"),
        ("revert_timed_out_tasks", "revert_timed_out"),
        ("revert_completed_silent_tasks", "revert_completed_silent"),
        ("park_terminal_sibling_tasks", "terminal_sibling"),
        ("run_lost_session_recovery", "lost_session"),
        ("run_concierge_recoveries", "concierge"),
        ("run_escalation_sweep", "escalation"),
    )
    _LOCKED = (
        ("record_session_liveness_changes", "liveness"),
        ("run_shipped_release", "shipped_release"),
        ("sweep_routed_result_sessions", "routed_page"),
    )

    def _spy(
        self, monkeypatch: pytest.MonkeyPatch, names: tuple[tuple[str, str], ...]
    ) -> list[str]:
        order: list[str] = []
        for name, label in names:
            _spy_core(monkeypatch, order, name, label)
        return order

    def test_tail_order_without_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._spy(monkeypatch, self._TAIL)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        reconcile()

        assert order == [label for _name, label in self._TAIL]

    def test_tail_order_with_phantoms(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
        save_state(CwState(sessions=[_mk_session("s1", "missing-ref")]))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        order = self._spy(monkeypatch, self._TAIL)

        report = reconcile()

        assert report.phantom_session_ids == ["s1"]
        assert order == [label for _name, label in self._TAIL]

    def test_shipped_release_runs_once_before_the_page_sweep(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._spy(monkeypatch, self._LOCKED)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        reconcile()

        assert order == ["liveness", "shipped_release", "routed_page"]

    def test_neither_runs_on_the_daemon_outage_early_return(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[_mk_session("s1", "live-ref")]))
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        order = self._spy(
            monkeypatch,
            (
                ("run_lost_session_recovery", "lost_session"),
                ("run_shipped_release", "shipped_release"),
            ),
        )

        reconcile()

        assert order == []

    def test_locked_body_without_probes_passes_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        def _record(*_args: object, **kwargs: object) -> list[str]:
            seen.append(kwargs["probes"])
            return []

        monkeypatch.setattr(locked_sweeps, "run_shipped_release", _record)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        with sessions_lock():
            reconcile_core._reconcile_locked(
                deferred=DeferredReconcileJobs(),
                roster_snapshot=fresh_roster_snapshot(),
            )

        assert seen == [None]


# ---------------------------------------------------------------------------
# The straight-line sweep groups, called directly (#2896)
# ---------------------------------------------------------------------------


def _record_calls(
    monkeypatch: pytest.MonkeyPatch, steps: dict[str, object]
) -> list[tuple[str, dict[str, object]]]:
    """Stub each ``locked_sweeps.<name>`` in *steps*, returning its value.

    Every call appends ``(name, kwargs)`` to the returned list, in call order.
    """
    calls: list[tuple[str, dict[str, object]]] = []
    for name, result in steps.items():

        def _stub(
            *_args: object,
            _name: str = name,
            _result: object = result,
            **kwargs: object,
        ) -> object:
            calls.append((_name, kwargs))
            return _result

        monkeypatch.setattr(locked_sweeps, name, _stub)
    return calls


class TestRunPreGuardSweeps:
    def test_runs_the_roster_independent_sweeps_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        calls = _record_calls(
            monkeypatch,
            {
                "_detect_stalled_candidates": [],
                "_act_on_stalled_candidates": None,
                "_detect_local_harvest_candidates": [],
                "_act_on_local_harvest_candidates": ["T-1"],
                "_detect_main_drift_candidates": [],
                "_act_on_main_drift_candidates": None,
                "sweep_leaked_daemon_workers": None,
            },
        )
        deferred = DeferredReconcileJobs()
        codex_probes = CleanProbes()
        harvest_facts = HarvestFacts()

        now_value = datetime(2026, 1, 1, tzinfo=UTC)
        clients_value = {
            "client-a": ClientConfig(name="client-a", workspace_path=Path())
        }
        harvested = locked_sweeps._run_pre_guard_sweeps(
            CwState(sessions=[]),
            now=now_value,
            config=OrchestratorConfig(),
            clients=clients_value,
            tasks=[],
            task_by_ticket={},
            deferred=deferred,
            codex_probes=codex_probes,
            harvest_facts=harvest_facts,
            drift_probes=None,
        )

        assert harvested == ["T-1"]
        assert [name for name, _kw in calls] == [
            "_detect_stalled_candidates",
            "_act_on_stalled_candidates",
            "_detect_local_harvest_candidates",
            "_act_on_local_harvest_candidates",
            "_detect_main_drift_candidates",
            "_act_on_main_drift_candidates",
            "sweep_leaked_daemon_workers",
        ]
        kwargs = dict(calls)
        harvest_kwargs = kwargs["_act_on_local_harvest_candidates"]
        assert harvest_kwargs["codex_probes"] is codex_probes
        assert harvest_kwargs["harvest_facts"] is harvest_facts
        assert kwargs["sweep_leaked_daemon_workers"]["daemon"] is daemon
        assert kwargs["sweep_leaked_daemon_workers"]["deferred"] is deferred
        # #2670: the absent-arm evidence (now, clients) reaches the sweep as given.
        assert kwargs["sweep_leaked_daemon_workers"]["now"] is now_value
        assert kwargs["sweep_leaked_daemon_workers"]["clients"] is clients_value


class TestRunPostGuardSweeps:
    def test_runs_the_roster_dependent_sweeps_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        effective = {"client-b": ClientConfig(name="client-b", workspace_path=Path())}
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients", lambda: effective
        )
        calls = _record_calls(
            monkeypatch,
            {
                "_backfill_claude_session_ids": 0,
                "_verify_supervisor_session_id": 0,
                "_stamp_session_id_mismatch_advisories": None,
                "_detect_idle_candidates": [],
                "_act_on_idle_candidates": None,
                "record_session_liveness_changes": None,
                "run_shipped_release": [],
                "sweep_routed_result_sessions": None,
                "detect_and_park_mid_turn_usage_limits": ["T-9"],
            },
        )
        clients = {"client-a": ClientConfig(name="client-a", workspace_path=Path())}
        shipped_probes = MagicMock()
        scope_probes = new_scope_probes()
        captured_at = datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)

        reverted = locked_sweeps._run_post_guard_sweeps(
            CwState(sessions=[]),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            native_live={"live-ref"},
            surface_to_full={"live-ref": "live-ref-full"},
            config=OrchestratorConfig(),
            clients=clients,
            tasks=[],
            task_by_ticket={},
            deferred=DeferredReconcileJobs(),
            scope_probes=scope_probes,
            shipped_probes=shipped_probes,
            roster_captured_at=captured_at,
        )

        assert reverted == ["T-9"]
        assert [name for name, _kw in calls] == [
            "_backfill_claude_session_ids",
            "_verify_supervisor_session_id",
            "_stamp_session_id_mismatch_advisories",
            "_detect_idle_candidates",
            "_act_on_idle_candidates",
            "record_session_liveness_changes",
            "run_shipped_release",
            "sweep_routed_result_sessions",
            "detect_and_park_mid_turn_usage_limits",
        ]
        kwargs = dict(calls)
        assert kwargs["_detect_idle_candidates"]["scope_probes"] is scope_probes
        assert kwargs["run_shipped_release"]["probes"] is shipped_probes
        assert kwargs["run_shipped_release"]["clients"] is clients
        assert set(kwargs["sweep_routed_result_sessions"]["enabled_clients"]) == {
            "client-a"
        }
        assert kwargs["detect_and_park_mid_turn_usage_limits"]["clients"] is effective
        assert (
            kwargs["detect_and_park_mid_turn_usage_limits"]["roster_captured_at"]
            == captured_at
        )


class TestWithoutActsInFlight:
    def test_empty_input_reads_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _unreadable() -> DevQueueStore:
            msg = "the dev queue must not be read for an empty phantom set"
            raise AssertionError(msg)

        monkeypatch.setattr(locked_sweeps, "load_dev_queue", _unreadable)

        assert locked_sweeps._without_acts_in_flight([]) == []

    def test_drops_sessions_whose_row_carries_an_act(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="T-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="s-acting",
                        usage_limit_act=UsageLimitAct(
                            session_id="s-acting",
                            branch="auto",
                            started_at=now,
                            reset_at=None,
                            until=now + timedelta(minutes=30),
                            audited_at=now,
                        ),
                    )
                ]
            )
        )

        kept = locked_sweeps._without_acts_in_flight(["s-acting", "s-phantom"])

        assert kept == ["s-phantom"]
