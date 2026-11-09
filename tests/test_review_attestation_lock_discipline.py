"""ADR-0019 lock discipline of the attestation seam (#3040, RFC 0017 II1).

Both call sites can run with ``sessions_lock`` held: the gate-recipe approve
(``reconcile`` -> ``run_gate_recipes`` -> ``_stamp_review_approval``) and the
idle-sentinel router (``_act_on_idle_candidates`` -> ``_stage_advance_unchecked``).
Each test holds the lock itself, as the production caller does, under a
strict trace with an empty allowlist, and asserts no subprocess launched.
"""

from __future__ import annotations

import ast
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.config import clients_file, get_client, save_state, sessions_lock
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue import review_attestation as builder_mod
from cw.dev_queue.lifecycle import _clear_review_approval, _record_review_approval
from cw.models import (
    CwState,
    DevQueueStore,
    LastResultSource,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.branch import resolve_feature_branch
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.gate_plan_probes import PlanProbes
from cw.reconcile.gate_recipes import run_gate_recipes
from cw.reconcile.idle import _act_on_idle_candidates, _detect_idle_candidates
from cw.reconcile.idle import _detect as idle_detect
from cw.worktree import resolve_task_worktree, worktree_path_for
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._lock_invariants import (
    SUBPROCESS_UNDER_SESSIONS_ALLOWLIST,
    LockTrace,
    install,
)
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _stage_complete_payload,
)
from tests._review_attestation_helpers import (
    attestation_for,
    clean_sentinel,
    converged_worktree_ledger,
    large_review_pending_result,
    review_row,
)
from tests._ticket_view_helpers import bound_session_for

SHA = "c" * 40
OTHER = "d" * 40
LOGGER = "cw.dev_queue.review_attestation"

# Copies of private names in tests/test_reconcile_gate_recipes.py (_GATE_LANES,
# _NOW, _config) and tests/test_reconcile_idle.py (the staged-client and
# emit-state fixtures). Dedupe is tracked debt (see the #3040 friction report).
GATE_LANES: list[dict[str, object]] = [
    {
        "name": lane,
        "gate_recipes": {
            "auto_approve_clean_review": True,
            "auto_adopt_clean_plan": True,
        },
    }
    for lane in ("default", "fastlane")
]
GATE_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
_IDLE_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_IDLE_NOW = _IDLE_STARTED_AT + timedelta(minutes=10)
_IDLE_TICKET = "salv-1"
_IDLE_CLIENT = "client-a"


def gate_config(**kwargs: object) -> OrchestratorConfig:
    return OrchestratorConfig.model_validate({"gate_recipes_enabled": True, **kwargs})


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> LockTrace:
    """A trace with an empty allowlist, chained in front of the harness's."""
    return install(monkeypatch, allowlist={})


def _assert_no_subprocess(trace: LockTrace) -> None:
    assert SUBPROCESS_UNDER_SESSIONS_ALLOWLIST == {}
    assert trace.subprocess_calls() == ()
    trace.assert_clean()


def _derived_worktree(client_name: str, ticket_id: str) -> Path:
    client = get_client(client_name)
    worktree = worktree_path_for(client, resolve_feature_branch(ticket_id, client))
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


# ---------------------------------------------------------------------------
# Test A: the gate-recipe approve path
# ---------------------------------------------------------------------------


def _seed_gate_row(tmp_path: Path, *, convergence: str, ledger_sha: str | None) -> None:
    extra: dict[str, object] = {"review": {"convergence": convergence}}
    write_clients_yaml(
        ClientSpec(
            "acme",
            tmp_path,
            default_branch="main",
            worktree_base=tmp_path / "wts",
            lanes=GATE_LANES,
            extra=extra,
        )
    )
    if ledger_sha is not None:
        converged_worktree_ledger(
            _derived_worktree("acme", "GEN-1"), ledger_sha, "GEN-1"
        )
    task = review_row(
        None,
        status=QueueItemStatus.BLOCKED_ON_USER,
        ticket_id="GEN-1",
        client="acme",
        session_id="sess-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))
    session = bound_session_for(
        task,
        surface_ref=None,
        worktree_path=None,
        started_at=GATE_NOW,
        last_result=large_review_pending_result(SHA),
    )
    save_state(CwState(sessions=[session]))


def _run_gate_recipes_locked() -> list[str]:
    sink = DeferredReconcileJobs()
    with sessions_lock():
        approved = run_gate_recipes(
            now=GATE_NOW, config=gate_config(), deferred=sink, plan_probes=PlanProbes()
        )
    run_post_lock_jobs(sink)
    return approved


@pytest.mark.parametrize(
    ("convergence", "ledger_sha"), [("blocking", None), ("clean", SHA)]
)
def test_gate_recipe_approve_attests_with_no_subprocess(
    tmp_config_dir: Path,
    tmp_path: Path,
    trace: LockTrace,
    convergence: str,
    ledger_sha: str | None,
) -> None:
    _seed_gate_row(tmp_path, convergence=convergence, ledger_sha=ledger_sha)

    assert _run_gate_recipes_locked() == ["GEN-1"]

    row = load_dev_queue().tasks[0]
    assert row.review_approver_kind == "recipe"
    att = row.review_attestation
    assert att is not None
    assert att.head_sha == SHA
    assert (att.counts.must_fix, att.counts.should_fix) == (0, 0)
    if convergence == "blocking":
        assert att.deferrals is None
        assert att.findings is None
    else:
        assert att.deferrals == []
    _assert_no_subprocess(trace)


def test_gate_recipe_disagreeing_ledger_writes_nothing(
    tmp_config_dir: Path,
    tmp_path: Path,
    trace: LockTrace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=LOGGER)
    _seed_gate_row(tmp_path, convergence="clean", ledger_sha=OTHER)

    assert _run_gate_recipes_locked() == ["GEN-1"]

    row = load_dev_queue().tasks[0]
    assert row.review_approver_kind == "recipe"
    assert row.review_attestation is None
    skips = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert len(skips) == 1
    assert "reason=ledger_disagrees" in skips[0]
    _assert_no_subprocess(trace)


# ---------------------------------------------------------------------------
# Test B: the idle-sentinel router path
# ---------------------------------------------------------------------------


@pytest.fixture
def idle_daemon(monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    return daemon


@pytest.fixture
def stub_git_review_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the three pre-existing git-measuring REVIEW gates to pass.

    Their own git under ``sessions_lock`` is pre-existing and out of scope
    here: run unstubbed, the harness attributes a ``git`` launch to
    ``cw.dispatch.review_gates`` (via ``resolve_task_worktree``'s branch
    probe), never to the attestation seam, and with no worktree the
    staleness gate parks the row instead. Tracked as debt in the #3040
    friction report. These tests pin only that II1's seam adds no subprocess.
    """
    for name in (
        "_should_gate_for_empty_diff",
        "_should_gate_for_branch_staleness",
        "_should_gate_for_review_staleness",
    ):
        monkeypatch.setattr(f"cw.dispatch.routing.{name}", lambda *_a, **_k: False)


@pytest.fixture
def no_transcript_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(_session: object, **_kw: object) -> None:
        pytest.fail("emit_cli candidate must not re-parse the transcript")

    monkeypatch.setattr(idle_detect, "_parse_any_sentinel_from_transcript", _fail)


def staged_review_client(tmp_path: Path, *, convergence: str) -> None:
    path = clients_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "clients:\n"
        f"  {_IDLE_CLIENT}:\n"
        f"    workspace_path: {tmp_path / 'ws'}\n"
        f"    worktree_base: {tmp_path / 'wts'}\n"
        "    default_branch: main\n"
        "    pipeline:\n"
        "      stages: [plan, impl, review, finalize]\n"
        "    review:\n"
        f"      convergence: {convergence}\n"
    )


def seed_review_row() -> None:
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_IDLE_TICKET,
                    client=_IDLE_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-1",
                    stage=Stage.REVIEW,
                    attempts=1,
                )
            ]
        )
    )


def review_stage_complete_payload(reviewed_sha: str, status: str) -> dict[str, object]:
    payload: dict[str, object] = {
        **_stage_complete_payload(),
        "ticket_id": _IDLE_TICKET,
        "status": status,
        "stage_reached": "stage3_review",
    }
    sentinel = clean_sentinel(reviewed_sha, status=status)
    payload["review"] = sentinel["review"]
    health = payload["health"]
    assert isinstance(health, dict)
    payload["health"] = {**health, "recommendation": "PROCEED"}
    return payload


def review_emit_state(tmp_path: Path, payload: dict[str, object]) -> CwState:
    sess = _mk_headless_daemon_session("salv-1", tmp_path / "wt", _IDLE_STARTED_AT)
    sess.name = f"{_IDLE_CLIENT}/auto-dev/{_IDLE_TICKET}"
    sess.last_result = payload
    sess.last_result_source = LastResultSource.EMIT_CLI
    return CwState(sessions=[sess])


@pytest.mark.parametrize(
    ("status", "convergence"),
    [
        ("stage_complete", "blocking"),
        ("stage_complete", "clean"),
        ("review_pending_approval", "blocking"),
    ],
)
def test_idle_router_advance_attests_with_no_subprocess(
    tmp_config_dir: Path,
    tmp_path: Path,
    trace: LockTrace,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
    stub_git_review_gates: None,
    status: str,
    convergence: str,
) -> None:
    """Rule 3 and Rule 1 advances attest under the lock, no allowlist (#3040)."""
    staged_review_client(tmp_path, convergence=convergence)
    if convergence == "clean":
        converged_worktree_ledger(
            _derived_worktree(_IDLE_CLIENT, _IDLE_TICKET), SHA, _IDLE_TICKET
        )
    seed_review_row()
    state = review_emit_state(tmp_path, review_stage_complete_payload(SHA, status))
    candidates = _detect_idle_candidates(
        state,
        now=_IDLE_NOW,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    assert len(candidates) == 1

    sink = DeferredReconcileJobs()
    with sessions_lock():
        _act_on_idle_candidates(state, candidates, now=_IDLE_NOW, deferred=sink)
    run_post_lock_jobs(sink)

    row = next(t for t in load_dev_queue().tasks if t.ticket_id == _IDLE_TICKET)
    assert row.stage == Stage.FINALIZE, (row.status, row.disposition)
    assert row.review_attestation is not None
    assert row.review_attestation.head_sha == SHA
    _assert_no_subprocess(trace)


# ---------------------------------------------------------------------------
# Test C: refresh-only and clear under the lock
# ---------------------------------------------------------------------------


def test_refresh_and_clear_under_the_lock(
    tmp_config_dir: Path, trace: LockTrace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=LOGGER)
    task = review_row(None)
    at = datetime(2026, 11, 8, tzinfo=UTC)
    with sessions_lock():
        _record_review_approval(task, "operator", SHA, at)
        assert task.review_attestation is None
        task.review_attestation = attestation_for(SHA)
        _clear_review_approval(task, path="stage_regress")
    assert task.review_attestation is None
    skips = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert len(skips) == 1
    assert "reason=refresh_no_attestation" in skips[0]
    _assert_no_subprocess(trace)


# ---------------------------------------------------------------------------
# Test D: structural
# ---------------------------------------------------------------------------


def test_builder_imports_no_git_seam() -> None:
    tree = ast.parse(Path(builder_mod.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {
        "subprocess",
        "cw._git",
        "cw.gh",
        "cw.branch_ahead",
        "cw.merge_probe",
    }


def test_resolve_without_checkout_launches_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        msg = "no subprocess allowed"
        raise AssertionError(msg)

    monkeypatch.setattr(subprocess, "Popen", _boom)
    from tests._review_attestation_helpers import clean_client_wt

    client, worktree = clean_client_wt(tmp_path, "T-1")
    task = review_row(None)
    assert task.worktree_path is None
    assert resolve_task_worktree(task, client, verify_checkout=False) == worktree
