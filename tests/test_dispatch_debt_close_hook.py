"""Tests for the dispatch-loop review-debt close sweep step (#3044)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cw._lock_guard import held_locks
from cw.dispatch import run_dispatch_loop
from cw.dispatch.loop import _run_debt_close_sweep_guarded
from cw.exceptions import CwError, CwLockOrderError, CwLockReentrancyError
from cw.models import OrchestratorConfig
from cw.review_debt_filing import (
    DEBT_CLOSE_SWEEP_INTERVAL_S,
    CloseOutcome,
    CloseResult,
    DebtCloseState,
)
from tests._clients_yaml import write_clients_yaml
from tests._review_debt_helpers import FakeClock, _register_debt_client, _sweep_state
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cw.models import ClientConfig

_SWEEP = "cw.dispatch.loop.sweep_debt_closes"


def _recording_sweep(
    calls: list[Mapping[str, ClientConfig]],
    results: list[CloseResult] | None = None,
) -> Callable[..., list[CloseResult]]:
    def _sweep(
        clients: Mapping[str, ClientConfig], _state: DebtCloseState, **_kw: object
    ) -> list[CloseResult]:
        calls.append(clients)
        return list(results or [])

    return _sweep


def _quiet_loop(monkeypatch: pytest.MonkeyPatch, client: ClientConfig) -> None:
    write_clients_yaml(client)
    monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", lambda **_kw: None)
    monkeypatch.setattr("cw.dispatch.loop._DEBT_CLOSE_STATE", _sweep_state())


class TestGuardedStep:
    def test_switch_off_runs_no_sweep_and_leaves_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Mapping[str, ClientConfig]] = []
        monkeypatch.setattr(_SWEEP, _recording_sweep(calls))
        state = _sweep_state()
        config = OrchestratorConfig(review_debt_close_enabled=False)
        _run_debt_close_sweep_guarded(config, state)
        assert calls == []
        assert state.next_due is None

    def test_the_interval_gate_runs_once_per_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Mapping[str, ClientConfig]] = []
        monkeypatch.setattr(_SWEEP, _recording_sweep(calls))
        clock = FakeClock(start=100.0)
        state = _sweep_state(clock)
        config = OrchestratorConfig()
        _run_debt_close_sweep_guarded(config, state)
        _run_debt_close_sweep_guarded(config, state)
        clock.now += DEBT_CLOSE_SWEEP_INTERVAL_S - 1
        _run_debt_close_sweep_guarded(config, state)
        assert len(calls) == 1
        clock.now += 1
        _run_debt_close_sweep_guarded(config, state)
        assert len(calls) == 2
        assert state.next_due == 100.0 + 2 * DEBT_CLOSE_SWEEP_INTERVAL_S

    def test_the_default_state_uses_the_monotonic_clock(self) -> None:
        state = DebtCloseState()
        assert state.claim() is True
        assert state.claim() is False

    @pytest.mark.parametrize(
        "exc",
        [
            CwError("store"),
            OSError("io"),
            ValueError("bad"),
            TypeError("payload"),
            KeyError("field"),
            yaml.YAMLError("clients.yaml"),
            RuntimeError("unexpected"),
        ],
    )
    def test_an_operational_failure_is_logged_and_the_gate_still_advances(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exc: Exception,
    ) -> None:
        raised: list[Exception] = []

        def _boom(*_a: object, **_k: object) -> list[CloseResult]:
            raised.append(exc)
            raise exc

        monkeypatch.setattr(_SWEEP, _boom)
        state = _sweep_state()
        with caplog.at_level(logging.ERROR):
            _run_debt_close_sweep_guarded(OrchestratorConfig(), state)
            _run_debt_close_sweep_guarded(OrchestratorConfig(), state)
        assert raised == [exc]
        assert state.next_due == DEBT_CLOSE_SWEEP_INTERVAL_S
        assert any("close sweep failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("kind", [CwLockReentrancyError, CwLockOrderError])
    def test_lock_discipline_errors_are_reraised(
        self, monkeypatch: pytest.MonkeyPatch, kind: type[CwError]
    ) -> None:
        path = Path("/tmp/dev_queue.lock")
        error: CwError
        if kind is CwLockReentrancyError:
            error = CwLockReentrancyError(
                "reentry", lock_name="dev_queue", path=path, held=("dev_queue",)
            )
        else:
            error = CwLockOrderError(
                "order",
                lock_name="dev_queue",
                path=path,
                rank_name="DEV_QUEUE",
                held=("leaf",),
            )

        def _raise(*_a: object, **_k: object) -> list[CloseResult]:
            raise error

        monkeypatch.setattr(_SWEEP, _raise)
        with pytest.raises(kind):
            _run_debt_close_sweep_guarded(OrchestratorConfig(), _sweep_state())

    def test_a_sweep_that_closed_something_logs_at_info(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        results = [
            CloseResult("acme", 1, ("u",), CloseOutcome.CLOSED),
            CloseResult("acme", 2, ("u",), CloseOutcome.SKIPPED_PR_OPEN),
        ]
        monkeypatch.setattr(_SWEEP, _recording_sweep([], results))
        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            _run_debt_close_sweep_guarded(OrchestratorConfig(), _sweep_state())
        assert "closed 1 debt ticket(s)" in caplog.text

    def test_a_sweep_that_closed_nothing_is_quiet(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(_SWEEP, _recording_sweep([]))
        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            _run_debt_close_sweep_guarded(OrchestratorConfig(), _sweep_state())
        assert "close sweep" not in caplog.text

    def test_the_real_sweep_scopes_gh_to_each_clients_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        _register_debt_client(root, name="acme", tracker="github-issues")
        cli = FakeGhCli().install(monkeypatch)
        cli.reply("gh", "issue", "list", stdout=json.dumps([]))
        _run_debt_close_sweep_guarded(OrchestratorConfig(), _sweep_state())
        assert [record.argv[:3] for record in cli.records] == [["gh", "issue", "list"]]
        assert cli.records[0].cwd == root
        assert cli.lock_snapshots == [()]


class TestRunDispatchLoopDebtCloseHook:
    def test_the_sweep_runs_after_hydration_and_the_shipped_red_scan(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        order: list[str] = []
        monkeypatch.setattr(
            "cw.dispatch.loop.hydrate_pr_states", lambda _c: order.append("hydrate")
        )
        monkeypatch.setattr(
            "cw.dispatch.loop.scan_shipped_red_prs", lambda _c: order.append("red")
        )

        def _sweep(*_a: object, **_k: object) -> list[CloseResult]:
            order.append("close")
            return []

        monkeypatch.setattr(_SWEEP, _sweep)
        run_dispatch_loop(once=True, emit=None)
        assert order == ["hydrate", "red", "close"]

    def test_raising_earlier_steps_do_not_stop_the_sweep(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)

        def _boom(_config: object) -> None:
            msg = "gh crashed"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.dispatch.loop.hydrate_pr_states", _boom)
        monkeypatch.setattr("cw.dispatch.loop.scan_shipped_red_prs", _boom)
        calls: list[Mapping[str, ClientConfig]] = []
        monkeypatch.setattr(_SWEEP, _recording_sweep(calls))
        run_dispatch_loop(once=True, emit=None)
        assert len(calls) == 1
        assert list(calls[0]) == [sample_client_config.name]

    def test_the_sweep_runs_with_no_guarded_lock_held(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        held_at_call: list[tuple[object, ...]] = []

        def _sweep(*_a: object, **_k: object) -> list[CloseResult]:
            held_at_call.append(tuple(held_locks()))
            return []

        monkeypatch.setattr(_SWEEP, _sweep)
        run_dispatch_loop(once=True, emit=None)
        assert held_at_call == [()]

    def test_the_disabled_switch_skips_the_sweep_in_the_loop(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        monkeypatch.setattr(
            "cw.dispatch.loop.load_effective_config",
            lambda: OrchestratorConfig(review_debt_close_enabled=False),
        )
        calls: list[Mapping[str, ClientConfig]] = []
        monkeypatch.setattr(_SWEEP, _recording_sweep(calls))
        run_dispatch_loop(once=True, emit=None)
        assert calls == []
