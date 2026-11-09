"""Where the attestation builder is called from (#3040, RFC 0017 II1).

The REVIEW clean auto-advance (Rule 1 and Rule 3, through
``_stage_advance_unchecked``) and ``approve`` (through
``_stamp_review_approval``). The three git-measuring REVIEW gates are stubbed
to pass (or to park) so the routing is in-memory and git-free.
"""

from __future__ import annotations

import ast
import inspect
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import approve_ticket, must_fix_override, review_less_release
from cw.dispatch import apply_staged_decision
from cw.models import ClientConfig, OrchestratorEventType, QueueItemStatus, Stage
from cw.models.orchestrator_config import StagePipelineConfig
from tests._review_attestation_helpers import (
    clean_sentinel,
    converged_worktree_ledger,
    large_review_pending_result,
    review_row,
    with_engine_config,
    write_round_summary,
)
from tests._review_engine_helpers import make_summary
from tests._ticket_view_helpers import (
    bound_session_for,
    events_of,
    install_acme_client,
    make_blocked,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from cw.models import TicketTask

SHA = "e" * 40
_GATES = (
    "_should_gate_for_empty_diff",
    "_should_gate_for_branch_staleness",
    "_should_gate_for_review_staleness",
)


def _stub_gates(monkeypatch: pytest.MonkeyPatch, parked: str | None = None) -> None:
    for name in _GATES:
        value = name == parked
        monkeypatch.setattr(
            f"cw.dispatch.routing.{name}", lambda *_a, _v=value, **_k: _v
        )


def _clients(
    tmp_path: Path, *, stages: list[Stage] | None = None
) -> dict[str, ClientConfig]:
    pipeline = StagePipelineConfig(
        stages=stages or [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
    )
    client = ClientConfig(
        name="test-client", workspace_path=tmp_path, pipeline=pipeline
    )
    return {client.name: client}


def _small(result: dict[str, object]) -> dict[str, object]:
    result["scope"] = {
        "tier": "small",
        "files": 2,
        "lines_estimate": 20,
        "lines_actual": 20,
        "forbidden_touched": False,
    }
    return result


@pytest.mark.parametrize(
    "result",
    [
        clean_sentinel(SHA, status="stage_complete"),
        _small(clean_sentinel(SHA, status="review_pending_approval")),
    ],
    ids=["rule3_stage_complete", "rule1_small_tier"],
)
def test_clean_review_advance_attests_then_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: dict[str, object]
) -> None:
    _stub_gates(monkeypatch)
    task = review_row(None)
    status = result["status"]
    assert isinstance(status, str)
    apply_staged_decision(task, status, result, _clients(tmp_path))
    assert task.stage == Stage.FINALIZE
    assert task.review_attestation is not None
    assert task.review_attestation.head_sha == SHA
    assert len(events_of(OrchestratorEventType.REVIEW_ATTESTATION_RECORDED)) == 1


def _engine_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, summary: bool
) -> TicketTask:
    """Advance a clean REVIEW row on an engine client; return the advanced row."""
    _stub_gates(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    if summary:
        write_round_summary(worktree, "round-1", make_summary(head_sha=SHA))
    task = review_row(worktree)
    converged_worktree_ledger(worktree, SHA, task.ticket_id)
    engine = with_engine_config(_clients(tmp_path)["test-client"])
    apply_staged_decision(
        task, "stage_complete", clean_sentinel(SHA), {engine.name: engine}
    )
    assert task.stage == Stage.FINALIZE
    return task


def test_engine_round_summary_reaches_the_routed_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _engine_advance(tmp_path, monkeypatch, summary=True)
    assert task.review_attestation is not None
    assert task.review_attestation.engine == "code-review"
    assert task.review_attestation.risk == "LOW"


def test_engine_client_without_a_summary_advances_ledger_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _engine_advance(tmp_path, monkeypatch, summary=False)
    assert task.review_attestation is not None
    assert task.review_attestation.engine is None
    assert task.review_attestation.acceptance is None


@pytest.mark.parametrize(
    "parked",
    ["_should_gate_for_empty_diff", "_should_gate_for_review_staleness", "health"],
)
def test_parked_review_gate_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parked: str
) -> None:
    _stub_gates(monkeypatch, parked=parked)
    result = (
        clean_sentinel(SHA, recommendation="EXIT_FOR_HUMAN_REVIEW")
        if parked == "health"
        else clean_sentinel(SHA)
    )
    task = review_row(None)
    apply_staged_decision(task, "stage_complete", result, _clients(tmp_path))
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.stage == Stage.REVIEW
    assert task.review_attestation is None


def test_non_review_advance_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gates(monkeypatch)
    task = review_row(None, stage=Stage.IMPL)
    apply_staged_decision(
        task, "stage_complete", clean_sentinel(SHA), _clients(tmp_path)
    )
    assert task.stage == Stage.REVIEW
    assert task.review_attestation is None


def test_terminal_review_stage_attests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_gates(monkeypatch)
    task = review_row(None)
    clients = _clients(tmp_path, stages=[Stage.PLAN, Stage.IMPL, Stage.REVIEW])
    apply_staged_decision(task, "stage_complete", clean_sentinel(SHA), clients)
    assert task.status == QueueItemStatus.COMPLETED
    assert task.review_attestation is not None


def test_sentinel_without_review_block_advances_but_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``review`` block: the staleness gate is exempt, and nothing attests."""
    for name in _GATES[:2]:
        monkeypatch.setattr(f"cw.dispatch.routing.{name}", lambda *_a, **_k: False)
    result = clean_sentinel(SHA)
    del result["review"]
    task = review_row(None)
    apply_staged_decision(task, "stage_complete", result, _clients(tmp_path))
    assert task.stage == Stage.FINALIZE
    assert task.review_attestation is None


def _seed_approvable(result: dict[str, object]) -> None:
    task = make_blocked(stage=Stage.REVIEW, disposition="review_pending_approval")
    seed_queue([task], [bound_session_for(task, last_result=result)])


def test_operator_approve_of_large_park_attests(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _seed_approvable(large_review_pending_result(SHA))
    approve_ticket("ACM-1", "acme")
    row = queue_row()
    assert row.review_approver_kind == "operator"
    assert row.review_approved_sha == SHA
    assert row.review_attestation is not None
    assert row.review_attestation.head_sha == row.review_approved_sha


def test_operator_approve_of_degraded_park_writes_no_attestation(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_acme_client(tmp_path, monkeypatch)
    result = large_review_pending_result(SHA)
    result["health"] = {"recommendation": "EXIT_FOR_HUMAN_REVIEW"}
    _seed_approvable(result)
    approve_ticket("ACM-1", "acme")
    row = queue_row()
    assert row.review_approved_sha == SHA
    assert row.review_attestation is None


@pytest.mark.parametrize("module", [must_fix_override, review_less_release])
def test_override_and_release_callers_only_refresh(module: ModuleType) -> None:
    """They call the stamping primitive without ``fresh_review``."""
    tree = ast.parse(inspect.getsource(module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_record_review_approval"
    ]
    assert calls
    for call in calls:
        assert "fresh_review" not in {kw.arg for kw in call.keywords}
