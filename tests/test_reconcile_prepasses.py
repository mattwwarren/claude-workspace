"""Unit tests for cw.reconcile.prepasses — the lockless pre-lock captures.

Moved out of ``tests/test_reconcile_core.py`` with the #2896 split. The
``reconcile()``-driven tests here patch each capture's collaborators on
``cw.reconcile.prepasses`` (the module that reads them) and the in-lock
consumers on ``cw.reconcile.locked_sweeps``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import freezegun
import pytest
from pydantic import ValidationError

from cw._lock_guard import LockRank, is_rank_held
from cw.config import (
    dev_queue_file,
    load_clients,
    load_state,
    save_state,
    sessions_lock,
    state_file,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    PrState,
    QueueItemStatus,
    ReapPolicy,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import (
    ReconcileReport,
    _deps,
    codex_boot,
    dirty_checks,
    locked_sweeps,
    prepasses,
    reconcile,
    roster_snapshot,
)
from cw.reconcile import core as reconcile_core
from cw.reconcile._shared import (
    ReapCandidate,
    ScopeProbes,
    new_scope_probes,
)
from cw.reconcile.cancelled_row_probes import new_cancelled_row_probes
from cw.reconcile.codex_boot import CAPTURE_BUDGET_SECONDS, CleanProbe, CleanProbes
from cw.reconcile.deferred import DeferredReconcileJobs
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    DirtyChecks,
)
from cw.reconcile.gate_plan_probes import (
    PLAN_PREFETCH_BUDGET_SECONDS,
    PLAN_PREFETCH_MAX_PER_TICK,
    PlanProbes,
)
from cw.reconcile.harvest_synthesis import HARVEST_CAPTURE_BUDGET_SECONDS, HarvestFacts
from cw.reconcile.main_drift import new_main_drift_probes
from tests._dirty_check_helpers import (
    DIRTY_CHECKS_LOGGER,
    fresh_roster_snapshot,
    probe_events,
    raising_helper,
    unavailable_records,
)
from tests._reconcile_helpers import (
    SCOPE_GUARD_FILES,
    SCOPE_GUARD_LINES,
    _attention_events,
    _auto_config,
    _inflate_scope,
    _make_stale_base_repo,
    _mk_headless_daemon_session,
    _mk_session,
    _shipped_salvage_payload,
    _write_salvage_transcript,
    probe_sessions_lock_free,
)
from tests.conftest import _make_daemon_session, _make_ticket_task
from tests.test_reconcile_codex_reparks import _seed_two_client_parks
from tests.test_reconcile_gate_recipes import _make_session as _gate_session
from tests.test_reconcile_gate_recipes import _make_task as _gate_task
from tests.test_reconcile_gate_recipes import _plan_result


def test_reconcile_prepass_uses_fresh_merged_pr_state_without_gh_call(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: a phantom DAEMON session whose task carries a fresh
    MERGED pr_state is treated as merged (completed_reason=NORMAL, not
    CRASHED) WITHOUT any _deps.pr_is_merged_for_ticket call."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-MERGED"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-MERGED",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="MERGED", hydrated_at=datetime.now(UTC) - timedelta(seconds=10)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.NORMAL


def test_reconcile_prepass_uses_fresh_open_pr_state_without_gh_call(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: fresh OPEN pr_state -> not merged, and no gh call is made."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-OPEN"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-OPEN",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="OPEN", hydrated_at=datetime.now(UTC) - timedelta(seconds=10)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    def _should_not_be_called(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        msg = "pr_is_merged_for_ticket must not be called when pr_state is fresh"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket", _should_not_be_called
    )
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.CRASHED


def test_reconcile_prepass_falls_back_to_gh_when_pr_state_stale(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub #975: stale pr_state falls back to the ordinary gh call path
    unchanged (existing pre-#975 behavior)."""
    monkeypatch.setattr("cw.reconcile.core.load_orchestrator_config", _auto_config)
    sess = _mk_session("sess-daemon", "dead-ref")
    sess.origin = SessionOrigin.DAEMON
    sess.name = "client-a/auto-dev/TKT-STALE"
    save_state(CwState(sessions=[sess]))

    task = _make_ticket_task(
        ticket_id="TKT-STALE",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        pr_state=PrState(
            state="MERGED", hydrated_at=datetime.now(UTC) - timedelta(seconds=300)
        ),
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    calls: list[str] = []

    def _capture(_tid: str, **_kw: object) -> tuple[bool | None, bool]:
        calls.append(_tid)
        return True, True

    monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "decoy000"}],
    )
    reconcile()

    assert calls == ["TKT-STALE"]
    reloaded = load_state()
    session = reloaded.find_by_name_or_id("sess-daemon")
    assert session is not None
    assert session.completed_reason == CompletionReason.NORMAL


# reconcile()'s dev-queue loads through cw.reconcile.prepasses' binding: its
# gh merge-state pre-pass first, then the codex
# clean-probe pre-pass (#2563), then the gate recipes' plan prefetch
# pre-pass third (#2545). The review recipes' repo-slug pre-pass (#2564) loads
# through its own module's binding, so it is not counted here; the dirty-check
# pre-pass (#2548) is the fourth, and the local harvest-facts pre-pass (#2565)
# the fifth, last before the lock.
_CODEX_PRE_PASS_LOAD = 2
_PLAN_PRE_PASS_LOAD = 3
_DIRTY_PRE_PASS_LOAD = 4
_HARVEST_PRE_PASS_LOAD = 5


class TestCodexCleanProbePrePass:
    """#2563: reconcile() captures the codex clean probes lockless and threads
    them to both in-lock consumers; a read error defers, never fails."""

    def test_pre_pass_probes_are_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2563: reconcile() captures the codex clean probes before it takes
        sessions_lock, and hands that one object to both in-lock consumers."""
        save_state(CwState(sessions=[]))
        sentinel = CleanProbes()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(
            *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
        ) -> CleanProbes:
            del config, clients
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(
            reconcile_core, "_capture_codex_clean_probes", _fake_capture
        )
        repark_mock = MagicMock(return_value=[])
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_codex_live_writer_reparks", repark_mock
        )
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps._act_on_local_harvest_candidates", harvest_mock
        )

        reconcile()

        assert lock_free_at_capture == [True]
        assert repark_mock.call_args.kwargs["probes"] is sentinel
        assert harvest_mock.call_args.kwargs["codex_probes"] is sentinel

    def test_unreadable_state_in_the_pre_pass_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2563 containment: reconcile() also serves cw start/cw doctor --reap, so
        a pre-pass read failure must not fail it. Every codex candidate gets an
        empty CleanProbes (a miss) and defers."""
        save_state(CwState(sessions=[]))
        real_load = prepasses.load_dev_queue
        loads: list[int] = []

        def _second_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _CODEX_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(prepasses, "load_dev_queue", _second_load_fails)
        repark_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps.run_codex_live_writer_reparks", repark_mock
        )

        with caplog.at_level(logging.WARNING, logger=prepasses.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        probes = repark_mock.call_args.kwargs["probes"]
        assert isinstance(probes, CleanProbes)
        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert "codex clean-probe pre-pass could not read state" in caplog.text


def _validation_error() -> ValidationError:
    try:
        CwState.model_validate({"sessions": 5})
    except ValidationError as err:
        return err
    msg = "expected a ValidationError"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    "error",
    [
        OSError("state unreadable"),
        json.JSONDecodeError("bad", "{", 0),
        _validation_error(),
    ],
    ids=["os-error", "json-decode-error", "validation-error"],
)
def test_capture_pre_pass_contains_state_read_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def _fail() -> CwState:
        raise error

    monkeypatch.setattr(prepasses, "load_state", _fail)

    probes = prepasses._capture_codex_clean_probes(
        config=OrchestratorConfig(), clients={}
    )

    assert probes.captured_keys == frozenset()
    assert probes.budget_seconds is None


def test_capture_pre_pass_does_not_swallow_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broad except: only the expected read errors are contained."""

    def _fail() -> CwState:
        msg = "a bug, not an unreadable file"
        raise RuntimeError(msg)

    monkeypatch.setattr(prepasses, "load_state", _fail)

    with pytest.raises(RuntimeError, match="a bug"):
        prepasses._capture_codex_clean_probes(config=OrchestratorConfig(), clients={})


def test_capture_pre_pass_spends_at_most_its_budget(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-pass runs within CAPTURE_BUDGET_SECONDS: once a slow capture
    spends it, later candidates get no probe (they defer in-lock)."""
    _seed_two_client_parks(
        tmp_config_dir,
        tmp_path,
        make_git_repo,
        lane_policies={"client-a": ReapPolicy.AUTO, "client-b": ReapPolicy.AUTO},
    )
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: [])
    clock = {"now": 0.0}
    monkeypatch.setattr(codex_boot, "monotonic", lambda: clock["now"])
    real_probe = codex_boot.probe_clean_state

    def _slow_probe(*args: Any) -> CleanProbe:
        probe = real_probe(*args)
        clock["now"] += CAPTURE_BUDGET_SECONDS + 1
        return probe

    monkeypatch.setattr(codex_boot, "probe_clean_state", _slow_probe)

    probes = prepasses._capture_codex_clean_probes(
        config=OrchestratorConfig(reap_policy=ReapPolicy.AUTO),
        clients=load_clients(),
    )

    assert probes.budget_seconds == CAPTURE_BUDGET_SECONDS
    assert len(probes.captured_keys) == 1


class TestPlanPrefetchPrePass:
    """#2545: the gate recipes' plan-of-record read runs in a lockless
    pre-pass, and the in-lock run_gate_recipes only reads its probes."""

    @pytest.mark.parametrize("phantom", [False, True], ids=["no-phantoms", "phantom"])
    def test_plan_pre_pass_is_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch, phantom: bool
    ) -> None:
        if phantom:
            save_state(CwState(sessions=[_mk_session("phantom-1", "missing-ref")]))
            monkeypatch.setattr(
                "cw.reconcile.core._claude_agents_json",
                lambda: [{"sessionId": "unrelated1"}],
            )
        else:
            save_state(CwState(sessions=[]))
        sentinel = PlanProbes()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(
            *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
        ) -> PlanProbes:
            del config, clients
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(reconcile_core, "_capture_plan_probes", _fake_capture)
        gate_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.locked_sweeps.run_gate_recipes", gate_mock)

        report = reconcile()

        assert report.phantom_session_ids == (["phantom-1"] if phantom else [])
        assert lock_free_at_capture == [True]
        gate_mock.assert_called_once()
        assert gate_mock.call_args.kwargs["plan_probes"] is sentinel

    def test_plan_pre_pass_is_bounded_by_the_explicit_cap_and_budget(self) -> None:
        save_state(CwState(sessions=[]))

        probes = prepasses._capture_plan_probes(
            config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
        )

        assert probes.max_captures == PLAN_PREFETCH_MAX_PER_TICK
        assert probes.budget_seconds == PLAN_PREFETCH_BUDGET_SECONDS

    @pytest.mark.parametrize(
        "error",
        [
            OSError("state unreadable"),
            json.JSONDecodeError("bad", "{", 0),
            _validation_error(),
        ],
        ids=["os-error", "json-decode-error", "validation-error"],
    )
    def test_capture_plan_pre_pass_contains_state_read_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        def _fail() -> CwState:
            raise error

        monkeypatch.setattr(prepasses, "load_state", _fail)

        with caplog.at_level(logging.WARNING, logger=prepasses.__name__):
            probes = prepasses._capture_plan_probes(
                config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
            )

        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert probes.max_captures is None
        assert "plan prefetch pre-pass could not read state" in caplog.text

    def test_capture_plan_pre_pass_does_not_swallow_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only state/dev-queue read errors are contained: a reader bug
        propagates out of the pre-pass, as it did from the old in-lock read."""
        save_dev_queue(DevQueueStore(tasks=[_gate_task(stage=Stage.PLAN)]))
        save_state(CwState(sessions=[_gate_session(last_result=_plan_result())]))

        def _boom(_task: TicketTask, _client_cfg: ClientConfig | None) -> str | None:
            msg = "a reader bug, not an unreadable file"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile.gate_recipes._plan_of_record_body", _boom)

        with pytest.raises(RuntimeError, match="a reader bug"):
            prepasses._capture_plan_probes(
                config=OrchestratorConfig(gate_recipes_enabled=True), clients={}
            )

    def test_capture_plan_pre_pass_reads_nothing_when_gate_recipes_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[str] = []

        def _spy() -> CwState:
            reads.append("state")
            return CwState(sessions=[])

        monkeypatch.setattr(prepasses, "load_state", _spy)

        probes = prepasses._capture_plan_probes(
            config=OrchestratorConfig(gate_recipes_enabled=False), clients={}
        )

        assert reads == []
        assert probes.captures == 0
        assert probes.max_captures is None

    def test_unreadable_dev_queue_in_the_plan_pre_pass_does_not_fail_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = prepasses.load_dev_queue
        loads: list[int] = []

        def _third_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _PLAN_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(prepasses, "load_dev_queue", _third_load_fails)
        gate_mock = MagicMock(return_value=[])
        monkeypatch.setattr("cw.reconcile.locked_sweeps.run_gate_recipes", gate_mock)

        with caplog.at_level(logging.WARNING, logger=prepasses.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        probes = gate_mock.call_args.kwargs["plan_probes"]
        assert isinstance(probes, PlanProbes)
        assert probes.captured_keys == frozenset()
        assert probes.budget_seconds is None
        assert "plan prefetch pre-pass could not read state" in caplog.text


class TestHarvestFactsPrePass:
    """#2565: the local harvest's git facts are captured in reconcile()'s last
    lockless pre-pass, and the in-lock act only looks them up."""

    def test_harvest_facts_captured_unlocked_and_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        sentinel = HarvestFacts()
        lock_free_at_capture: list[bool] = []

        def _fake_capture() -> HarvestFacts:
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(prepasses, "_capture_harvest_facts", _fake_capture)
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps._act_on_local_harvest_candidates", harvest_mock
        )

        reconcile()

        assert lock_free_at_capture == [True]
        assert harvest_mock.call_args.kwargs["harvest_facts"] is sentinel

    def test_unreadable_state_in_harvest_pre_pass_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = prepasses.load_dev_queue
        loads: list[int] = []

        def _harvest_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _HARVEST_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(prepasses, "load_dev_queue", _harvest_load_fails)
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps._act_on_local_harvest_candidates", harvest_mock
        )

        with caplog.at_level(logging.WARNING, logger=prepasses.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        facts = harvest_mock.call_args.kwargs["harvest_facts"]
        assert isinstance(facts, HarvestFacts)
        assert facts.budget_seconds is None
        assert "harvest-facts pre-pass could not read state" in caplog.text

    def test_capture_harvest_pre_pass_does_not_swallow_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> CwState:
            msg = "a bug, not an unreadable file"
            raise RuntimeError(msg)

        monkeypatch.setattr(prepasses, "load_state", _fail)

        with pytest.raises(RuntimeError, match="a bug"):
            prepasses._capture_harvest_facts()

    def test_capture_harvest_pre_pass_builds_a_budgeted_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        driver = MagicMock()
        monkeypatch.setattr(prepasses, "capture_local_harvest_facts", driver)

        facts = prepasses._capture_harvest_facts()

        assert facts.budget_seconds == HARVEST_CAPTURE_BUDGET_SECONDS
        assert driver.call_args.kwargs["facts"] is facts

    def test_reconcile_locked_without_harvest_facts_passes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        harvest_mock = MagicMock(return_value=[])
        monkeypatch.setattr(
            "cw.reconcile.locked_sweeps._act_on_local_harvest_candidates", harvest_mock
        )

        with sessions_lock():
            reconcile_core._reconcile_locked(
                deferred=DeferredReconcileJobs(),
                roster_snapshot=fresh_roster_snapshot(),
            )

        assert harvest_mock.call_args.kwargs["harvest_facts"] is None


class TestShippedCapturePrePass:
    """#2715: the shipped-release gh pre-pass is lockless and precedes the
    harvest capture."""

    def test_shipped_capture_is_lockless_and_precedes_harvest(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[tuple[str, bool]] = []
        real_capture = prepasses.capture_shipped_pr_probes
        real_harvest = prepasses._capture_harvest_facts

        def _capture(**kwargs: Any) -> Any:
            order.append(("shipped", is_rank_held(LockRank.SESSIONS)))
            return real_capture(**kwargs)

        def _harvest() -> HarvestFacts:
            order.append(("harvest", is_rank_held(LockRank.SESSIONS)))
            return real_harvest()

        monkeypatch.setattr(prepasses, "capture_shipped_pr_probes", _capture)
        monkeypatch.setattr(prepasses, "_capture_harvest_facts", _harvest)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        reconcile()

        assert order == [("shipped", False), ("harvest", False)]


class TestDriftAndCancelledPrePass:
    """#2546: the main-drift and recipe-3 git reads are captured lockless,
    after the shipped pass and before the harvest pass, and the two stores
    reach their in-lock consumers by identity."""

    def test_capture_is_lockless_between_shipped_and_harvest(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[tuple[str, bool]] = []
        for name, label in (
            ("capture_shipped_pr_probes", "shipped"),
            ("capture_drift_and_cancelled_probes", "drift_and_cancelled"),
            ("capture_salvage_scope_probes", "salvage_scope"),
            ("_capture_harvest_facts", "harvest"),
        ):
            real = getattr(prepasses, name)

            def _wrapped(
                *args: Any, _real: Any = real, _label: str = label, **kwargs: Any
            ) -> Any:
                order.append((_label, probe_sessions_lock_free()))
                return _real(*args, **kwargs)

            monkeypatch.setattr(prepasses, name, _wrapped)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        reconcile()

        assert order == [
            ("shipped", True),
            ("drift_and_cancelled", True),
            ("salvage_scope", True),
            ("harvest", True),
        ]

    @staticmethod
    def _record_consumers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
        seen: dict[str, list[object]] = {"drift": [], "concierge": []}
        real_detect = locked_sweeps._detect_main_drift_candidates

        def _detect(*args: Any) -> Any:
            seen["drift"].append(args[2])
            return real_detect(*args)

        def _concierge(**kwargs: Any) -> list[str]:
            seen["concierge"].append(kwargs["cancelled_probes"])
            return []

        monkeypatch.setattr(locked_sweeps, "_detect_main_drift_candidates", _detect)
        monkeypatch.setattr(locked_sweeps, "run_concierge_recoveries", _concierge)
        return seen

    @pytest.mark.parametrize("with_phantom", [True, False])
    def test_stores_are_threaded_by_identity(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        with_phantom: bool,
    ) -> None:
        """Both terminal tails (phantom and no-phantom) forward the store."""
        sessions = [_mk_session("s1", "missing-ref")] if with_phantom else []
        save_state(CwState(sessions=sessions))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        drift_store = new_main_drift_probes()
        cancelled_store = new_cancelled_row_probes()
        monkeypatch.setattr(
            prepasses,
            "capture_drift_and_cancelled_probes",
            lambda **_kw: (drift_store, cancelled_store),
        )
        seen = self._record_consumers(monkeypatch)

        report = reconcile()

        assert (report.phantom_session_ids == ["s1"]) is with_phantom
        assert len(seen["drift"]) == 1
        assert seen["drift"][0] is drift_store
        assert len(seen["concierge"]) == 1
        assert seen["concierge"][0] is cancelled_store

    def test_locked_body_without_probes_passes_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_consumers(monkeypatch)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        with sessions_lock():
            reconcile_core._reconcile_locked(
                deferred=DeferredReconcileJobs(),
                roster_snapshot=fresh_roster_snapshot(),
            )

        assert seen == {"drift": [None], "concierge": [None]}


class TestSalvageScopePrePass:
    """#2641: the salvaged-sentinel scope measurement is captured lockless,
    between the drift/cancelled pass and the harvest pass, and the store
    reaches every in-lock sentinel parse by identity."""

    @staticmethod
    def _record_consumers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
        seen: dict[str, list[object]] = {"idle": [], "phantom": [], "concierge": []}
        real_idle = locked_sweeps._detect_idle_candidates
        real_phantom = reconcile_core._detect_phantom_candidates

        def _idle(*args: Any, **kwargs: Any) -> Any:
            seen["idle"].append(kwargs.get("scope_probes"))
            return real_idle(*args, **kwargs)

        def _phantom(*args: Any, **kwargs: Any) -> Any:
            seen["phantom"].append(kwargs.get("scope_probes"))
            return real_phantom(*args, **kwargs)

        def _concierge(**kwargs: Any) -> list[str]:
            seen["concierge"].append(kwargs.get("scope_probes"))
            return []

        monkeypatch.setattr(locked_sweeps, "_detect_idle_candidates", _idle)
        monkeypatch.setattr(reconcile_core, "_detect_phantom_candidates", _phantom)
        monkeypatch.setattr(locked_sweeps, "run_concierge_recoveries", _concierge)
        return seen

    @pytest.mark.parametrize("with_phantom", [True, False])
    def test_store_is_threaded_by_identity(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        with_phantom: bool,
    ) -> None:
        """Both terminal tails (phantom and no-phantom) forward the store."""
        sessions = [_mk_session("s1", "missing-ref")] if with_phantom else []
        save_state(CwState(sessions=sessions))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        store = new_scope_probes()
        monkeypatch.setattr(
            prepasses, "capture_salvage_scope_probes", lambda **_kw: store
        )
        seen = self._record_consumers(monkeypatch)

        report = reconcile()

        assert (report.phantom_session_ids == ["s1"]) is with_phantom
        assert len(seen["idle"]) == 1
        assert seen["idle"][0] is store
        assert len(seen["phantom"]) == (1 if with_phantom else 0)
        assert all(probes is store for probes in seen["phantom"])
        assert len(seen["concierge"]) == 1
        assert seen["concierge"][0] is store

    def test_locked_body_without_probes_passes_none(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_consumers(monkeypatch)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)

        with sessions_lock():
            reconcile_core._reconcile_locked(
                deferred=DeferredReconcileJobs(),
                roster_snapshot=fresh_roster_snapshot(),
            )

        assert seen == {"idle": [None], "phantom": [], "concierge": [None]}

    @staticmethod
    def _seed_phantom_scope(make_git_repo: Callable[..., Path]) -> Path:
        """A crashed phantom whose transcript carries an inflated shipped sentinel."""
        worktree = _make_stale_base_repo(make_git_repo, "wt-core-scope")
        sess = _mk_headless_daemon_session(
            "salv-scope", worktree, _SCOPE_STARTED_AT, surface_ref="gone-ref"
        )
        _write_scope_transcript(worktree, files=18)
        alive = _mk_session("alive", surface_ref="live-ref")
        save_state(CwState(sessions=[sess, alive]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="salv-scope",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="salv-scope",
                    )
                ]
            )
        )
        return worktree

    @staticmethod
    def _reconcile_now(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "live-ref"}],
        )
        with freezegun.freeze_time(_SCOPE_STARTED_AT + timedelta(minutes=5)):
            reconcile()

    @staticmethod
    def _salvaged() -> tuple[dict[str, Any], QueueItemStatus]:
        session = next(s for s in load_state().sessions if s.id == "salv-scope")
        assert session.status == SessionStatus.COMPLETED
        assert session.last_result is not None
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "salv-scope")
        return session.last_result["scope"], task.status

    def test_reconcile_corrects_salvaged_scope_without_in_lock_git(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Headline: real reconcile(), real git, no allowlist entry needed.

        The autouse lock harness fails this test on any git under
        sessions_lock; the allowlist is empty (#2641).
        """
        self._seed_phantom_scope(make_git_repo)

        self._reconcile_now(monkeypatch)

        scope, task_status = self._salvaged()
        assert scope["files"] == SCOPE_GUARD_FILES
        assert scope["lines_actual"] == SCOPE_GUARD_LINES
        assert task_status == QueueItemStatus.COMPLETED

    def test_sentinel_changed_after_capture_is_not_corrected(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A sentinel re-emitted between capture and apply keeps its own scope."""
        worktree = self._seed_phantom_scope(make_git_repo)
        real_capture = prepasses.capture_salvage_scope_probes

        def _capture_then_reemit(**kw: object) -> ScopeProbes:
            store = real_capture(**kw)
            _write_scope_transcript(worktree, files=40)
            return store

        monkeypatch.setattr(
            prepasses, "capture_salvage_scope_probes", _capture_then_reemit
        )

        with caplog.at_level(logging.WARNING):
            self._reconcile_now(monkeypatch)

        scope, task_status = self._salvaged()
        assert scope["files"] == 40
        assert scope["lines_actual"] == 1567
        assert task_status == QueueItemStatus.COMPLETED
        assert "salvaged_scope_probe_unavailable" in caplog.text
        assert "reason=changed" in caplog.text

    def test_empty_store_salvages_uncorrected(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._seed_phantom_scope(make_git_repo)
        monkeypatch.setattr(
            prepasses,
            "capture_salvage_scope_probes",
            lambda **_kw: new_scope_probes(),
        )

        with caplog.at_level(logging.WARNING):
            self._reconcile_now(monkeypatch)

        scope, task_status = self._salvaged()
        assert scope["files"] == 18
        assert scope["lines_actual"] == 1567
        assert task_status == QueueItemStatus.COMPLETED
        assert "reason=missing" in caplog.text


_SCOPE_STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _write_scope_transcript(worktree: Path, *, files: int) -> None:
    payload = _inflate_scope(_shipped_salvage_payload("salv-scope"))
    payload["scope"]["files"] = files
    _write_salvage_transcript(
        Path.home(), worktree, "claude-uuid-scope", payload, surface_ref="gone-ref"
    )


class TestDirtyChecksPrePass:
    """#2548: the worktree dirty checks are captured in a lockless pre-pass and
    the in-lock phantom detect and terminal backstops only look them up."""

    @staticmethod
    def _record_consumers(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        seen: dict[str, object] = {}
        real_detect = reconcile_core._detect_phantom_candidates

        def _detect(*args: Any, **kwargs: Any) -> list[ReapCandidate]:
            seen["detect"] = kwargs["dirty_checks"]
            return real_detect(*args, **kwargs)

        def _recorder(name: str) -> Callable[[DirtyChecks | None], list[str]]:
            def _revert(dirty_checks: DirtyChecks | None = None) -> list[str]:
                seen[name] = dirty_checks
                return []

            return _revert

        monkeypatch.setattr(reconcile_core, "_detect_phantom_candidates", _detect)
        monkeypatch.setattr(
            locked_sweeps, "revert_timed_out_tasks", _recorder("timed_out")
        )
        monkeypatch.setattr(
            locked_sweeps, "revert_completed_silent_tasks", _recorder("completed")
        )
        return seen

    @pytest.mark.parametrize("with_phantom", [True, False])
    def test_captured_unlocked_and_threaded_by_identity(
        self, monkeypatch: pytest.MonkeyPatch, with_phantom: bool
    ) -> None:
        sessions = [_mk_session("s1", "missing-ref")] if with_phantom else []
        save_state(CwState(sessions=sessions))
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": "decoy000"}],
        )
        sentinel = DirtyChecks()
        lock_free_at_capture: list[bool] = []

        def _fake_capture(
            *,
            config: OrchestratorConfig,
            roster: roster_snapshot.RosterReader,
            warned_blocks: object = None,
        ) -> DirtyChecks:
            del config, roster, warned_blocks
            lock_free_at_capture.append(probe_sessions_lock_free())
            return sentinel

        monkeypatch.setattr(reconcile_core, "_capture_dirty_checks", _fake_capture)
        seen = self._record_consumers(monkeypatch)

        reconcile()

        assert lock_free_at_capture == [True]
        assert seen.pop("timed_out") is sentinel
        assert seen.pop("completed") is sentinel
        if with_phantom:
            assert seen.pop("detect") is sentinel
        assert seen == {}

    def test_store_is_built_with_the_per_tick_bounds(self) -> None:
        save_state(CwState(sessions=[]))

        checks = prepasses._capture_dirty_checks(
            config=OrchestratorConfig(), roster=list
        )

        assert checks.budget_seconds == DIRTY_CHECK_BUDGET_SECONDS
        assert checks.max_captures == DIRTY_CHECK_MAX_PER_TICK
        assert checks.captures == 0

    def test_idle_tick_makes_no_roster_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No live DAEMON session with a worktree: no extra `claude agents`."""
        save_state(CwState(sessions=[_mk_session("s1", "live-ref")]))
        calls: list[int] = []

        def _roster() -> list[dict[str, object]]:
            calls.append(1)
            return []

        prepasses._capture_dirty_checks(config=OrchestratorConfig(), roster=_roster)

        assert calls == []

    def test_unreadable_state_defers_without_failing_reconcile(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        save_state(CwState(sessions=[]))
        real_load = prepasses.load_dev_queue
        loads: list[int] = []

        def _dirty_load_fails() -> DevQueueStore:
            loads.append(1)
            if len(loads) == _DIRTY_PRE_PASS_LOAD:
                msg = "dev queue unreadable"
                raise OSError(msg)
            return real_load()

        monkeypatch.setattr(prepasses, "load_dev_queue", _dirty_load_fails)
        seen = self._record_consumers(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=prepasses.__name__):
            report = reconcile()

        assert isinstance(report, ReconcileReport)
        checks = seen["timed_out"]
        assert isinstance(checks, DirtyChecks)
        assert checks.budget_seconds is None
        assert checks.max_captures is None
        assert "dirty-check pre-pass could not read state" in caplog.text

    def test_capture_does_not_swallow_unexpected_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only state/dev-queue read errors are contained: a reader bug
        propagates out of the pre-pass, as it did from the old in-lock check."""
        session = _make_daemon_session(
            id="to-1",
            name="client-a/auto-dev/GEN-1",
            status=SessionStatus.TIMED_OUT,
            worktree_path=tmp_path / "wt",
            surface_ref=None,
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="to-1",
                    )
                ]
            )
        )

        def _boom(_client: str, _path: object) -> str | None:
            msg = "a dirty-check bug"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._shared.worktree_dirty_reason_by_path", _boom)

        with pytest.raises(RuntimeError, match="a dirty-check bug"):
            prepasses._capture_dirty_checks(config=OrchestratorConfig(), roster=list)

    @staticmethod
    def _spy_settle(monkeypatch: pytest.MonkeyPatch) -> list[DirtyChecks]:
        """Wrap ``DirtyChecks.settle_outages``; returns the instances it ran on."""
        calls: list[DirtyChecks] = []
        real_settle = DirtyChecks.settle_outages

        def _spy(self: DirtyChecks) -> None:
            calls.append(self)
            real_settle(self)

        monkeypatch.setattr(DirtyChecks, "settle_outages", _spy)
        return calls

    def test_capture_dirty_checks_settles_outages_once_on_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2662: the edge latch and deferred order are settled once per tick."""
        save_state(CwState(sessions=[]))
        calls = self._spy_settle(monkeypatch)

        checks = prepasses._capture_dirty_checks(
            config=OrchestratorConfig(), roster=list
        )

        assert calls == [checks]

    def test_capture_dirty_checks_announces_a_probe_outage_once_and_mutates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2662: the production wiring signals a deferred probe, rows untouched.

        Runs the real ``_capture_dirty_checks`` (not a hand-called
        ``settle_outages``) over a seeded backstop session whose probe raises a
        ``git`` ``WorktreeProbeError``: exactly one ``worktree_probe_unavailable``
        event, and state and queue bytes and the RUNNING row unchanged.
        """
        session = _make_daemon_session(
            id="to-1",
            name="client-a/auto-dev/GEN-1",
            status=SessionStatus.TIMED_OUT,
            worktree_path=tmp_path / "wt-to-1",
            surface_ref=None,
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-1",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="to-1",
                    )
                ]
            )
        )
        raising_helper(monkeypatch, kind="git", detail="boom")
        before = (state_file().read_bytes(), dev_queue_file().read_bytes())

        checks = prepasses._capture_dirty_checks(
            config=OrchestratorConfig(), roster=list
        )

        assert checks.captures == 1
        (event,) = probe_events("probe")
        assert event["breadcrumbs"] == "git: boom"
        assert (state_file().read_bytes(), dev_queue_file().read_bytes()) == before
        assert load_state().sessions[0].status is SessionStatus.TIMED_OUT
        (row,) = load_dev_queue().tasks
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "to-1"

    def test_capture_dirty_checks_settles_after_the_phantom_pass_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2662: ``settle_outages`` runs after BOTH capture passes.

        The only candidate is a phantom-pass session (live DAEMON row absent
        from a non-empty roster), whose probe raises a ``git``
        ``WorktreeProbeError``. Settling between the backstop and phantom
        passes would see no outage and latch nothing, so the real
        ``_capture_dirty_checks`` must emit one event and record the deferral.
        """
        session = _make_daemon_session(
            id="ph-1",
            name="client-a/auto-dev/GEN-2",
            status=SessionStatus.ACTIVE,
            worktree_path=tmp_path / "wt-ph-1",
            surface_ref="missing-ref",
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-2",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="ph-1",
                    )
                ]
            )
        )
        raising_helper(monkeypatch, kind="git", detail="boom")

        checks = prepasses._capture_dirty_checks(
            config=OrchestratorConfig(), roster=lambda: [{"sessionId": "decoy000"}]
        )

        assert checks.captures == 1
        (event,) = probe_events("phantom-settle")
        assert event["breadcrumbs"] == "git: boom"
        assert {"ph-1"} == dirty_checks._DEFERRED_LAST_TICK

    def test_capture_dirty_checks_does_not_settle_when_state_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No evidence either way: both in-process sets are left untouched."""
        save_state(CwState(sessions=[]))
        dirty_checks._ANNOUNCED_OUTAGES["git"] = object()
        dirty_checks._DEFERRED_LAST_TICK.add("sess-1")
        calls = self._spy_settle(monkeypatch)

        def _unreadable() -> DevQueueStore:
            msg = "dev queue unreadable"
            raise OSError(msg)

        monkeypatch.setattr(prepasses, "load_dev_queue", _unreadable)

        checks = prepasses._capture_dirty_checks(
            config=OrchestratorConfig(), roster=list
        )

        assert checks.max_captures is None
        assert calls == []
        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert {"sess-1"} == dirty_checks._DEFERRED_LAST_TICK

    def test_reconcile_locked_without_dirty_checks_passes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(CwState(sessions=[]))
        seen = self._record_consumers(monkeypatch)

        with sessions_lock():
            reconcile_core._reconcile_locked(
                deferred=DeferredReconcileJobs(),
                roster_snapshot=fresh_roster_snapshot(),
            )

        assert seen == {"timed_out": None, "completed": None}

    def test_roster_race_session_defers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M8: live in the pre-pass roster, absent from the in-lock one, so it
        is a phantom with no capture: it defers a tick and is never parked.

        The two reads share one tick-scoped snapshot (#2550); a zero reuse
        window makes the final pre-lock refresh re-read, modeling a slow tick
        whose roster changed between the dirty-check pre-pass and the lock."""
        surface_ref = "racesess"
        session = _make_daemon_session(
            id="race-1",
            name="client-a/auto-dev/GEN-8",
            surface_ref=surface_ref,
            worktree_path=tmp_path / "wt-race",
        )
        save_state(CwState(sessions=[session]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _make_ticket_task(
                        ticket_id="GEN-8",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="race-1",
                    )
                ]
            )
        )
        rosters = iter(
            [[{"sessionId": f"{surface_ref}-full-uuid"}], [{"sessionId": "decoy000"}]]
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json", lambda: next(rosters)
        )
        monkeypatch.setattr(
            "cw.reconcile.roster_snapshot.ROSTER_REUSE_MAX_AGE_SECONDS", 0.0
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (False, True),
        )

        def _no_live_check(_client: str, _path: object) -> str | None:
            msg = "the roster-race session must not be dirty-checked"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_dirty_reason_by_path", _no_live_check
        )

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            report = reconcile()

        assert report.phantom_session_ids == ["race-1"]
        row = load_dev_queue().tasks[0]
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "race-1"
        assert load_state().sessions[0].status is SessionStatus.ACTIVE
        assert _attention_events("m8", "GEN-8") == []
        push = _deps.fire_push_notification
        assert isinstance(push, MagicMock)
        push.assert_not_called()
        assert len(unavailable_records(caplog, "race-1")) == 1


# ---------------------------------------------------------------------------
# _capture_merge_state: the gh merge-state pre-pass, called directly (#2896)
# ---------------------------------------------------------------------------


def _merge_session(ticket_id: str, **overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": f"sess-{ticket_id}",
        "name": f"client-a/auto-dev/{ticket_id}",
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _fake_pr_lookup(
    monkeypatch: pytest.MonkeyPatch,
    verdicts: dict[str, tuple[bool | None, bool]],
) -> list[str]:
    """Answer each ticket's gh lookup from *verdicts*; return the tickets asked."""
    asked: list[str] = []

    def _lookup(ticket_id: str, **_kw: object) -> tuple[bool | None, bool]:
        asked.append(ticket_id)
        return verdicts[ticket_id]

    monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _lookup)
    return asked


def _merge_clients(tmp_path: Path) -> dict[str, ClientConfig]:
    return {"client-a": ClientConfig(name="client-a", workspace_path=tmp_path)}


class TestCaptureMergeState:
    def test_splits_merged_from_unmerged_and_transient_tickets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merged lands in the first set; unmerged and merged=None in neither."""
        asked = _fake_pr_lookup(
            monkeypatch,
            {"T-1": (True, True), "T-2": (False, True), "T-3": (None, True)},
        )
        state = CwState(sessions=[_merge_session(t) for t in ("T-1", "T-2", "T-3")])

        merged, blocked = prepasses._capture_merge_state(
            state, clients=_merge_clients(tmp_path), config=OrchestratorConfig()
        )

        assert merged == frozenset({"T-1"})
        assert blocked == frozenset()
        assert asked == ["T-1", "T-2", "T-3"]

    def test_dangling_client_is_gh_blocked_without_a_gh_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client missing from a populated clients mapping never reaches gh."""
        asked = _fake_pr_lookup(monkeypatch, {})
        state = CwState(
            sessions=[
                _merge_session(
                    "T-9", client="client-gone", name="client-gone/auto-dev/T-9"
                )
            ]
        )

        merged, blocked = prepasses._capture_merge_state(
            state, clients=_merge_clients(tmp_path), config=OrchestratorConfig()
        )

        assert merged == frozenset()
        assert blocked == frozenset({"T-9"})
        assert asked == []

    def test_gh_outage_blocks_every_later_ticket_without_a_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = _fake_pr_lookup(monkeypatch, {"T-1": (None, False)})
        state = CwState(sessions=[_merge_session("T-1"), _merge_session("T-2")])

        merged, blocked = prepasses._capture_merge_state(
            state, clients=_merge_clients(tmp_path), config=OrchestratorConfig()
        )

        assert merged == frozenset()
        assert blocked == frozenset({"T-1", "T-2"})
        assert asked == ["T-1"]

    def test_skips_non_live_non_daemon_and_ticketless_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = _fake_pr_lookup(monkeypatch, {})
        state = CwState(
            sessions=[
                _merge_session("T-1", status=SessionStatus.COMPLETED),
                _merge_session("T-2", origin=SessionOrigin.USER),
                _merge_session("T-3", name="client-a/adhoc-T-3"),
            ]
        )

        merged, blocked = prepasses._capture_merge_state(
            state, clients=_merge_clients(tmp_path), config=OrchestratorConfig()
        )

        assert (merged, blocked) == (frozenset(), frozenset())
        assert asked == []

    def test_reads_the_dev_queue_even_with_no_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merge-state read is the tick's first dev-queue load."""
        loads: list[int] = []

        def _load() -> DevQueueStore:
            loads.append(1)
            return DevQueueStore(tasks=[])

        monkeypatch.setattr(prepasses, "load_dev_queue", _load)

        result = prepasses._capture_merge_state(
            CwState(sessions=[]), clients={}, config=OrchestratorConfig()
        )

        assert result == (frozenset(), frozenset())
        assert loads == [1]
