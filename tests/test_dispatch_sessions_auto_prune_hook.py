"""Tests for the dispatch-loop sessions auto-prune step (#2965)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw._lock_guard import held_locks
from cw.config import state_dir
from cw.dispatch import DispatchTickResult, run_dispatch_loop
from cw.dispatch.loop import _run_sessions_auto_prune_guarded
from cw.exceptions import CwError, CwLockOrderError, CwLockReentrancyError
from cw.models import OrchestratorConfig
from tests._clients_yaml import write_clients_yaml

if TYPE_CHECKING:
    from cw.models import ClientConfig


def _quiet_loop(monkeypatch: pytest.MonkeyPatch, client: ClientConfig) -> None:
    write_clients_yaml(client)
    monkeypatch.setattr("cw.dispatch.gating.usage_limit.reconcile", lambda **_kw: None)


class TestRunDispatchLoopSessionsAutoPruneHook:
    def test_loop_runs_auto_prune_after_dispatch_tick(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        order: list[str] = []
        monkeypatch.setattr(
            "cw.dispatch.loop.dispatch_tick",
            lambda *_a, **_k: (order.append("tick"), DispatchTickResult(spawned=0))[1],
        )
        monkeypatch.setattr(
            "cw.dispatch.loop.auto_prune_sessions",
            lambda _config: order.append("prune"),
        )

        run_dispatch_loop(once=True, emit=None)

        assert order == ["tick", "prune"]

    def test_auto_prune_runs_with_no_guarded_lock_held(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        held_at_call: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            "cw.dispatch.loop.auto_prune_sessions",
            lambda _config: held_at_call.append(tuple(held_locks())),
        )

        run_dispatch_loop(once=True, emit=None)

        assert held_at_call == [()]

    @pytest.mark.parametrize(
        "exc", [OSError("io"), ValueError("bad"), CwError("plain cw error")]
    )
    def test_guarded_step_swallows_expected_errors_and_loop_continues(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exc: Exception,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)

        def _boom(_config: object) -> None:
            raise exc

        monkeypatch.setattr("cw.dispatch.loop.auto_prune_sessions", _boom)

        with caplog.at_level("ERROR"):
            run_dispatch_loop(once=True, emit=None)

        assert any("auto-prune" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("kind", [CwLockReentrancyError, CwLockOrderError])
    def test_guarded_step_reraises_lock_discipline_errors(
        self, monkeypatch: pytest.MonkeyPatch, kind: type[CwError]
    ) -> None:
        path = Path("/tmp/sessions.lock")
        error: CwError
        if kind is CwLockReentrancyError:
            error = CwLockReentrancyError(
                "reentry", lock_name="sessions", path=path, held=("sessions",)
            )
        else:
            error = CwLockOrderError(
                "order",
                lock_name="sessions",
                path=path,
                rank_name="SESSIONS",
                held=("leaf",),
            )

        def _raise(_config: object) -> None:
            raise error

        monkeypatch.setattr("cw.dispatch.loop.auto_prune_sessions", _raise)

        with pytest.raises(kind):
            _run_sessions_auto_prune_guarded(OrchestratorConfig())

    def test_once_mode_runs_prune_when_due(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        configs: list[OrchestratorConfig] = []
        monkeypatch.setattr(
            "cw.dispatch.loop.auto_prune_sessions",
            configs.append,
        )

        run_dispatch_loop(once=True, emit=None)

        assert len(configs) == 1
        assert configs[0].sessions_auto_prune_enabled is True

    def test_disabled_config_does_not_prune(
        self,
        sample_client_config: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _quiet_loop(monkeypatch, sample_client_config)
        monkeypatch.setattr(
            "cw.dispatch.loop.load_effective_config",
            lambda: OrchestratorConfig(sessions_auto_prune_enabled=False),
        )

        def _must_not_run(**_kw: object) -> None:
            msg = "prune_sessions must not run when disabled"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.session_auto_prune.prune_sessions", _must_not_run)

        run_dispatch_loop(once=True, emit=None)

        assert not (state_dir() / "sessions_auto_prune.json").exists()
