"""Tests for cw.reconcile.lockless_prepass (#2546).

The wiring that loads state once, contains read errors, gates recipe 3's
capture on the concierge config, and captures both the main-drift and the
cancelled-row probes before ``sessions_lock``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.config import save_state
from cw.dev_queue import save_dev_queue
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    SessionStatus,
)
from cw.reconcile.cancelled_row_probes import CancelledRowKey
from cw.reconcile.concierge import run_concierge_recoveries
from cw.reconcile.lockless_prepass import capture_drift_and_cancelled_probes
from cw.reconcile.main_drift import MAIN_DRIFT_PROBE_BUDGET_SECONDS
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import _mk_daemon_session_with_worktree
from tests.conftest import _make_ticket_task

_MODULE = "cw.reconcile.lockless_prepass"


def _seed(tmp_path: Path) -> dict[str, ClientConfig]:
    """One live DAEMON session on client-a and one CANCELLED row on acme."""
    write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
    save_state(
        CwState(
            sessions=[
                _mk_daemon_session_with_worktree(
                    "s1", SessionStatus.ACTIVE, tmp_path / "wt"
                )
            ]
        )
    )
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="GEN-1",
                    client="acme",
                    status=QueueItemStatus.CANCELLED,
                    worktree_path=tmp_path,
                )
            ]
        )
    )
    return {"client-a": ClientConfig(name="client-a", workspace_path=tmp_path / "main")}


def _patch_git(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _classify(client: ClientConfig) -> str | None:
        calls.append(f"drift:{client.name}")
        return "dirty"

    def _commits(wt_path: Path, default_branch: str) -> bool:
        calls.append(f"commits:{wt_path.name}:{default_branch}")
        return True

    monkeypatch.setattr("cw.reconcile.main_drift._classify_main_drift", _classify)
    monkeypatch.setattr(
        "cw.reconcile.cancelled_row_probes._has_commits_beyond_base", _commits
    )
    return calls


def test_both_enabled_captures_both_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = _seed(tmp_path)
    calls = _patch_git(monkeypatch)

    result = capture_drift_and_cancelled_probes(
        config=OrchestratorConfig(concierge_enabled=True), clients=clients
    )

    assert isinstance(result, tuple)
    drift, cancelled = result
    assert isinstance(drift, BoundedProbeStore)
    assert isinstance(cancelled, BoundedProbeStore)
    assert drift.lookup("client-a") == "dirty"
    assert cancelled.lookup(CancelledRowKey("acme", "GEN-1", tmp_path, "main")) is True
    assert sorted(calls) == [f"commits:{tmp_path.name}:main", "drift:client-a"]


def test_slow_drift_pass_does_not_spend_recipe_3_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each store's budget starts at its own capture, not at pre-pass entry."""
    clients = _seed(tmp_path)
    calls = _patch_git(monkeypatch)
    now = [0.0]
    monkeypatch.setattr("cw.reconcile.probe_store.monotonic", lambda: now[0])

    def _slow_classify(client: ClientConfig) -> str | None:
        calls.append(f"drift:{client.name}")
        now[0] += MAIN_DRIFT_PROBE_BUDGET_SECONDS + 5.0
        return "dirty"

    monkeypatch.setattr("cw.reconcile.main_drift._classify_main_drift", _slow_classify)

    drift, cancelled = capture_drift_and_cancelled_probes(
        config=OrchestratorConfig(concierge_enabled=True), clients=clients
    )

    assert drift.lookup("client-a") == "dirty"
    assert cancelled.lookup(CancelledRowKey("acme", "GEN-1", tmp_path, "main")) is True
    assert f"commits:{tmp_path.name}:main" in calls


@pytest.mark.parametrize(
    "config",
    [
        OrchestratorConfig(concierge_enabled=False),
        OrchestratorConfig(
            concierge_enabled=True,
            concierge_recoveries={"cancelled_row_restore": False},
        ),
    ],
    ids=["concierge-disabled", "recipe-disabled"],
)
def test_recipe_3_gated_off_reads_no_queue_and_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: OrchestratorConfig
) -> None:
    clients = _seed(tmp_path)
    calls = _patch_git(monkeypatch)

    def _no_queue() -> DevQueueStore:
        msg = "dev queue must not be read when recipe 3 is off"
        raise AssertionError(msg)

    monkeypatch.setattr(f"{_MODULE}.load_dev_queue", _no_queue)

    drift, cancelled = capture_drift_and_cancelled_probes(
        config=config, clients=clients
    )

    assert cancelled.keys == frozenset()
    assert calls == ["drift:client-a"]
    assert drift.lookup("client-a") == "dirty"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (OrchestratorConfig(concierge_enabled=False), False),
        (OrchestratorConfig(concierge_enabled=True), True),
        (
            OrchestratorConfig(
                concierge_enabled=True,
                concierge_recoveries={"cancelled_row_restore": False},
            ),
            False,
        ),
        (
            OrchestratorConfig(
                concierge_enabled=True,
                concierge_recoveries={"cancelled_row_restore": True},
            ),
            True,
        ),
    ],
    ids=["concierge-off", "recipe-default", "recipe-off", "recipe-on"],
)
def test_capture_gate_and_consume_gate_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: OrchestratorConfig,
    expected: bool,
) -> None:
    """Recipe 3's lockless capture runs git iff the in-lock detect is consulted.

    The two gates live in different modules (``lockless_prepass`` decides
    whether to capture, ``run_concierge_recoveries`` whether to consume); if
    one gains a condition the other lacks, recipe 3 silently never restores or
    git runs for nothing. The literal ``expected`` keeps the test from passing
    vacuously when both sides drift the same way.
    """
    clients = _seed(tmp_path)
    calls = _patch_git(monkeypatch)
    detect_calls: list[object] = []

    def _record_detect(tasks: object, probes: object) -> list[object]:
        detect_calls.append(probes)
        return []

    monkeypatch.setattr(
        "cw.reconcile.concierge._detect_cancelled_row_candidates", _record_detect
    )

    capture_drift_and_cancelled_probes(config=config, clients=clients)
    run_concierge_recoveries(
        now=datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC),
        native_live=set(),
        config=config,
    )

    capture_ran_git = any(c.startswith("commits:") for c in calls)
    detect_consulted = bool(detect_calls)
    assert capture_ran_git == detect_consulted
    assert capture_ran_git is expected


@pytest.mark.parametrize(
    ("target", "exc"),
    [
        ("load_state", OSError("unreadable")),
        ("load_state", ValueError("corrupt")),
        ("load_dev_queue", OSError("unreadable")),
        ("load_dev_queue", ValueError("corrupt")),
    ],
)
def test_unreadable_state_or_queue_defers_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    target: str,
    exc: Exception,
) -> None:
    clients = _seed(tmp_path)
    calls = _patch_git(monkeypatch)

    def _raise() -> object:
        raise exc

    monkeypatch.setattr(f"{_MODULE}.{target}", _raise)

    with caplog.at_level(logging.WARNING, _MODULE):
        drift, cancelled = capture_drift_and_cancelled_probes(
            config=OrchestratorConfig(concierge_enabled=True), clients=clients
        )

    assert calls == []
    assert drift.keys == frozenset()
    assert cancelled.keys == frozenset()
    with pytest.raises(ProbeStoreUnavailableError):
        drift.lookup("client-a")
    with pytest.raises(ProbeStoreUnavailableError):
        cancelled.lookup(CancelledRowKey("acme", "GEN-1", tmp_path, "main"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "deferring both" in warnings[0].getMessage()


def test_unexpected_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = _seed(tmp_path)

    def _raise() -> object:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(f"{_MODULE}.load_state", _raise)

    with pytest.raises(RuntimeError, match="boom"):
        capture_drift_and_cancelled_probes(
            config=OrchestratorConfig(concierge_enabled=True), clients=clients
        )
