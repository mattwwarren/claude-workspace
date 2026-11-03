"""Tests for cw.reconcile.lockless_prepass (#2546, #2641).

The wiring that loads state once, contains read errors, gates recipe 3's
capture on the concierge config, and captures the main-drift, cancelled-row
and salvaged-scope probes before ``sessions_lock``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.config import save_state
from cw.dev_queue import save_dev_queue
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import new_scope_probes
from cw.reconcile._shared._scope_probes import ScopeKey
from cw.reconcile.cancelled_row_probes import CancelledRowKey
from cw.reconcile.concierge import run_concierge_recoveries
from cw.reconcile.lockless_prepass import (
    capture_drift_and_cancelled_probes,
    capture_salvage_scope_probes,
)
from cw.reconcile.main_drift import MAIN_DRIFT_PROBE_BUDGET_SECONDS
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import (
    _blocked_result_payload,
    _mk_daemon_session_with_worktree,
    _mk_headless_daemon_session,
    _no_op_salvage_payload,
    _stage_complete_payload,
    _write_salvage_transcript,
)
from tests.conftest import _make_daemon_session, _make_ticket_task

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
        "cw.reconcile.concierge.core._detect_cancelled_row_candidates", _record_detect
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


# ---------------------------------------------------------------------------
# #2641 — capture_salvage_scope_probes: the salvaged-sentinel scope check's
# git measurement, captured before sessions_lock.
# ---------------------------------------------------------------------------

_SCOPE_COMPUTE = "cw.reconcile._shared._scope_probes.compute_branch_diff_scope"


def _scope_result(
    payload: dict[str, Any] | None = None,
) -> AutoDevResult:
    return AutoDevResult.model_validate(
        payload if payload is not None else _stage_complete_payload()
    )


def _patch_scope_parse(
    monkeypatch: pytest.MonkeyPatch,
    by_session: dict[str, AutoDevResult | BlockedResult | None],
) -> list[tuple[str, object]]:
    """Stub the driver's parse; record each call's session id and scope_probes."""
    calls: list[tuple[str, object]] = []

    def _parse(
        session: Session, *, scope_probes: object = None
    ) -> tuple[AutoDevResult | BlockedResult, str] | None:
        calls.append((session.id, scope_probes))
        result = by_session.get(session.id)
        return None if result is None else (result, f"csid-{session.id}")

    monkeypatch.setattr(f"{_MODULE}._parse_any_sentinel_from_transcript", _parse)
    return calls


def _patch_scope_git(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    measured: list[Path] = []

    def _compute(worktree_path: Path, default_branch: str) -> dict[str, object]:
        measured.append(worktree_path)
        return {
            "branch": "b",
            "merge_base": "m",
            "files": 1,
            "lines_actual": 2,
        }

    monkeypatch.setattr(_SCOPE_COMPUTE, _compute)
    return measured


def _scope_session(sid: str, tmp_path: Path, **overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": sid,
        "name": f"client-a/auto-dev/{sid}",
        "worktree_path": tmp_path / f"wt-{sid}",
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def test_salvage_scope_captures_only_measurable_auto_dev_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pre_impl = _scope_result(_no_op_salvage_payload())
    blocked = BlockedResult.model_validate(_blocked_result_payload())
    sessions = [
        _scope_session("measured", tmp_path),
        _scope_session("idle", tmp_path, status=SessionStatus.IDLE),
        _scope_session("blocked", tmp_path),
        _scope_session("no-sentinel", tmp_path),
        _scope_session("pre-impl", tmp_path),
        _scope_session("done", tmp_path, status=SessionStatus.COMPLETED),
        _scope_session("user", tmp_path, origin=SessionOrigin.USER),
        _scope_session("no-wt", tmp_path, worktree_path=None),
    ]
    save_state(CwState(sessions=sessions))
    parse_calls = _patch_scope_parse(
        monkeypatch,
        {
            "measured": _scope_result(),
            "idle": _scope_result(),
            "blocked": blocked,
            "pre-impl": pre_impl,
            "done": _scope_result(),
            "user": _scope_result(),
            "no-wt": _scope_result(),
        },
    )
    measured = _patch_scope_git(monkeypatch)

    probes = capture_salvage_scope_probes()

    # Only live DAEMON sessions with a worktree are parsed, without a store.
    assert parse_calls == [
        ("measured", None),
        ("idle", None),
        ("blocked", None),
        ("no-sentinel", None),
        ("pre-impl", None),
    ]
    assert measured == [tmp_path / "wt-measured", tmp_path / "wt-idle"]
    assert probes.keys == frozenset(
        {
            ScopeKey("measured", tmp_path / "wt-measured"),
            ScopeKey("idle", tmp_path / "wt-idle"),
        }
    )
    assert probes.lookup(ScopeKey("measured", tmp_path / "wt-measured")).result == (
        _scope_result()
    )


def test_salvage_scope_budget_refusal_stops_with_one_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    save_state(CwState(sessions=[_scope_session(f"s{i}", tmp_path) for i in range(3)]))
    parse_calls = _patch_scope_parse(
        monkeypatch, {f"s{i}": _scope_result() for i in range(3)}
    )
    measured = _patch_scope_git(monkeypatch)
    # Wrap the REAL factory: a zero budget refuses the very first capture, via
    # production's own store (max_captures=None, so "budget" is the only
    # reachable refusal reason).
    real_factory = new_scope_probes
    monkeypatch.setattr(
        f"{_MODULE}.new_scope_probes", lambda: real_factory(budget_seconds=0.0)
    )

    with caplog.at_level(logging.WARNING, _MODULE):
        probes = capture_salvage_scope_probes()

    assert measured == []
    assert probes.keys == frozenset()
    # The loop returns at the first refusal: only s0 was ever parsed.
    assert [sid for sid, _ in parse_calls] == ["s0"]
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == _MODULE
    ]
    assert warnings == [
        "reconcile: salvage-scope probe capture stopped (budget); 0 captured,"
        " 3 of 3 session(s) deferred to the next tick"
    ]


def test_salvage_scope_mid_pass_budget_stop_keeps_earlier_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    save_state(CwState(sessions=[_scope_session(f"s{i}", tmp_path) for i in range(3)]))
    parse_calls = _patch_scope_parse(
        monkeypatch, {f"s{i}": _scope_result() for i in range(3)}
    )
    # Drive the REAL store's own 20 s budget with a fake clock: the first
    # measurement runs inside the budget, then "time passes" past the deadline,
    # so the second capture is the first refusal.
    clock = [0.0]
    monkeypatch.setattr("cw.reconcile.probe_store.monotonic", lambda: clock[0])
    measured: list[Path] = []

    def _compute(worktree_path: Path, default_branch: str) -> dict[str, object]:
        measured.append(worktree_path)
        clock[0] = 1000.0
        return {
            "branch": "b",
            "merge_base": "m",
            "files": 1,
            "lines_actual": 2,
        }

    monkeypatch.setattr(_SCOPE_COMPUTE, _compute)

    with caplog.at_level(logging.WARNING, _MODULE):
        probes = capture_salvage_scope_probes()

    # The earlier capture survives in the store the pass returns.
    assert measured == [tmp_path / "wt-s0"]
    assert probes.keys == frozenset({ScopeKey("s0", tmp_path / "wt-s0")})
    # The loop returns at the first refusal: s2 is never parsed.
    assert [sid for sid, _ in parse_calls] == ["s0", "s1"]
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == _MODULE
    ]
    assert warnings == [
        "reconcile: salvage-scope probe capture stopped (budget); 1 captured,"
        " 2 of 3 session(s) deferred to the next tick"
    ]


@pytest.mark.parametrize("exc", [OSError("unreadable"), ValueError("corrupt")])
def test_salvage_scope_unreadable_state_yields_empty_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    parse_calls = _patch_scope_parse(monkeypatch, {})

    def _raise() -> object:
        raise exc

    monkeypatch.setattr(f"{_MODULE}.load_state", _raise)

    with caplog.at_level(logging.WARNING, _MODULE):
        probes = capture_salvage_scope_probes()

    assert parse_calls == []
    assert probes.keys == frozenset()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "salvage-scope pre-pass could not read state" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


def test_salvage_scope_unexpected_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the state read is contained; a parse/read bug aborts the pass."""
    save_state(CwState(sessions=[_scope_session("s1", tmp_path)]))

    def _parse(session: Session, **_kw: object) -> object:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(f"{_MODULE}._parse_any_sentinel_from_transcript", _parse)

    with pytest.raises(RuntimeError, match="boom"):
        capture_salvage_scope_probes()


def test_salvage_scope_real_parse_measures_once(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's parse is verification-free: one measurement, not two."""
    worktree = tmp_path / "wt-real"
    worktree.mkdir()
    sess = _mk_headless_daemon_session(
        "real", worktree, datetime(2026, 1, 1, tzinfo=UTC)
    )
    sess.name = "client-a/auto-dev/salv-stage"
    _write_salvage_transcript(
        Path.home(), worktree, "claude-uuid-real", _stage_complete_payload()
    )
    save_state(CwState(sessions=[sess]))
    measured = _patch_scope_git(monkeypatch)

    def _forbid_raw_git(*args: object, **kwargs: object) -> object:
        msg = f"raw git ran during the pre-pass parse: {args!r}"
        raise AssertionError(msg)

    # A parse that verified through cw.worktree._scope would reach raw git on
    # this non-git dir and quietly return None, never touching `measured`.
    monkeypatch.setattr("cw.worktree._scope._run_git", _forbid_raw_git)
    # The branch probe runs its own subprocess.run, not _run_git (#2903).
    monkeypatch.setattr("cw.worktree._git.subprocess.run", _forbid_raw_git)

    probes = capture_salvage_scope_probes()

    assert measured == [worktree]
    assert probes.keys == frozenset({ScopeKey("real", worktree)})
