"""Claim-time review-capability preflight (#3790).

A PENDING REVIEW row whose lane's runner lacks a capability the client's
``review.engine`` requires is parked ``BLOCKED_ON_USER`` before any claim. The
park is uncharged: attempts, unproductive and regress counters are untouched,
the stage stays REVIEW, nothing is claimed. Releasing it is a config fix plus
``requeue`` (or a re-lane with ``move``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dev_queue.dispositions import (
    _PRE_DISPATCH_REVIEW_CAPABILITY_REASON,
    HOLD_DISPOSITIONS,
    REVIEW_CAPABILITY_UNMET_DISPOSITION,
)
from cw.dev_queue.gates import DRAIN_DISPOSITIONS
from cw.dispatch.claim.review_capability import (
    park_review_capability_unmet,
    review_capability_gap,
)
from cw.dispatch.claim.screening import _claim_next_pending
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.models.orchestrator_config.lane import LaneConfig

_CLIENT = "rc-client"
_TICKET = "RC-1"


def _client(
    tmp_path: Path,
    *,
    required: list[str],
    lanes: dict[str, list[str] | None],
    client_review: list[str] | None = None,
) -> ClientConfig:
    """A client whose lanes each declare (or omit) REVIEW runtime capabilities.

    ``lanes`` maps a lane name to its declared capabilities; ``None`` means the
    lane declares no REVIEW executor at all and inherits the client's.
    """
    lane_cfgs: list[LaneConfig] = []
    for name, caps in lanes.items():
        pipeline = None
        if caps is not None:
            pipeline = StagePipelineConfig(
                executors={Stage.REVIEW: StageExecutorConfig(runtime_capabilities=caps)}
            )
        lane_cfgs.append(LaneConfig(name=name, pipeline=pipeline))
    client_pipeline = StagePipelineConfig(
        executors={
            Stage.REVIEW: StageExecutorConfig(runtime_capabilities=client_review or [])
        }
    )
    return ClientConfig(
        name=_CLIENT,
        workspace_path=tmp_path,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        review=ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(
                skill="review-skill", requires_runtime_capabilities=required
            ),
        ),
        pipeline=client_pipeline,
        lanes=lane_cfgs,
    )


def _task(lane: str = "alpha", **overrides: Any) -> TicketTask:
    fields: dict[str, Any] = {
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "lane": lane,
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.PENDING,
        "branch": "dev/rc-1",
        "attempts": 3,
        "unproductive_attempts": 2,
        "regress_attempts": 1,
    }
    fields.update(overrides)
    return TicketTask.model_validate(fields)


def _seed(*rows: TicketTask) -> None:
    save_dev_queue(DevQueueStore(tasks=list(rows)))


def _stored() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _TICKET)


def _claim(client: ClientConfig, lane: str = "alpha") -> TicketTask | None:
    claimed, _ = _claim_next_pending(
        _CLIENT,
        lane=lane,
        client=client,
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 4}
        ),
        review_approval_holds=None,
    )
    return claimed


# -- pure gap function ------------------------------------------------------


def test_no_gap_without_an_engine(tmp_path: Path) -> None:
    client = ClientConfig(name=_CLIENT, workspace_path=tmp_path, default_branch="main")
    assert review_capability_gap(_task(), client) is None


def test_no_gap_when_the_engine_requires_nothing(tmp_path: Path) -> None:
    client = _client(tmp_path, required=[], lanes={"alpha": None})
    assert review_capability_gap(_task(), client) is None


def test_runner_superset_is_compatible(tmp_path: Path) -> None:
    client = _client(
        tmp_path, required=["jvm"], lanes={"alpha": ["cuda", "jvm", "zig"]}
    )
    assert review_capability_gap(_task(), client) is None


def test_no_gap_off_review(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": []})
    assert review_capability_gap(_task(stage=Stage.IMPL), client) is None


def test_gap_names_missing_declared_and_required_sorted(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        required=["jvm", "cuda", "zig"],
        lanes={"alpha": ["jvm"], "beta": ["cuda", "jvm", "zig"]},
    )
    gap = review_capability_gap(_task(), client)
    assert gap is not None
    assert gap.lane == "alpha"
    assert gap.required == ("cuda", "jvm", "zig")
    assert gap.declared == ("jvm",)
    assert gap.missing == ("cuda", "zig")
    assert gap.compatible_lanes == ("beta",)


def test_compatible_lanes_exclude_self_and_lanes_without_review(
    tmp_path: Path,
) -> None:
    no_review = StagePipelineConfig(stages=[Stage.PLAN, Stage.IMPL, Stage.FINALIZE])
    client = _client(
        tmp_path,
        required=["jvm"],
        lanes={"alpha": [], "gamma": ["jvm"], "delta": ["jvm"]},
    )
    # Re-declare delta without a REVIEW stage in its pipeline.
    client.lanes[2].pipeline = no_review
    gap = review_capability_gap(_task(), client)
    assert gap is not None
    assert gap.compatible_lanes == ("gamma",)


# -- claim-screen park -----------------------------------------------------


def test_claim_screen_parks_the_row_uncharged(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": []})
    pr_url = "https://example.test/pr/1"
    _seed(_task(pr_url=pr_url))

    assert _claim(client) is None

    parked = _stored()
    assert parked.status == QueueItemStatus.BLOCKED_ON_USER
    assert parked.disposition == REVIEW_CAPABILITY_UNMET_DISPOSITION
    assert parked.blocked_reason == _PRE_DISPATCH_REVIEW_CAPABILITY_REASON
    assert parked.pr_url == pr_url
    assert parked.stage == Stage.REVIEW
    assert parked.session_id is None
    assert parked.attempts == 3
    assert parked.unproductive_attempts == 2
    assert parked.regress_attempts == 1


def test_park_emits_the_attention_event(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": [], "beta": ["jvm"]})
    _seed(_task())

    _claim(client)

    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    payload = events[-1].payload
    assert payload["paused_status"] == REVIEW_CAPABILITY_UNMET_DISPOSITION
    assert payload["breadcrumbs"] == ""
    assert payload["lane"] == "alpha"
    assert payload["missing_capabilities"] == ["jvm"]
    assert payload["compatible_lanes"] == ["beta"]
    details = str(payload["details"])
    assert "clients.yaml" in details
    assert "runtime_capabilities" in details
    assert "cw dev-queue requeue" in details
    assert "cw dev-queue move" in details
    assert "--to beta" in details


def test_park_is_not_repeated_on_a_second_claim(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": []})
    _seed(_task())

    _claim(client)
    assert _claim(client) is None

    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(events) == 1


def test_compatible_runner_claims_normally(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": ["jvm"]})
    _seed(_task())

    claimed = _claim(client)

    assert claimed is not None
    assert claimed.ticket_id == _TICKET


def test_review_less_engineless_client_claims_normally(tmp_path: Path) -> None:
    client = ClientConfig(name=_CLIENT, workspace_path=tmp_path, default_branch="main")
    _seed(_task(lane="default"))

    assert _claim(client, lane="default") is not None


def test_parks_the_row_for_a_declared_gap_only_on_review(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": []})
    row = _task()
    _seed(row)
    store = load_dev_queue()
    stored = next(t for t in store.tasks if t.ticket_id == _TICKET)

    assert park_review_capability_unmet(stored, client, _CLIENT, "alpha", store)
    assert stored.disposition == REVIEW_CAPABILITY_UNMET_DISPOSITION


def test_park_helper_is_a_no_op_without_a_gap(tmp_path: Path) -> None:
    client = _client(tmp_path, required=["jvm"], lanes={"alpha": ["jvm"]})
    row = _task()
    _seed(row)
    store = load_dev_queue()
    stored = next(t for t in store.tasks if t.ticket_id == _TICKET)

    assert not park_review_capability_unmet(stored, client, _CLIENT, "alpha", store)
    assert stored.status == QueueItemStatus.PENDING


# -- negative and regression guards --------------------------------------


def test_disposition_is_not_a_hold_or_drain_member() -> None:
    assert REVIEW_CAPABILITY_UNMET_DISPOSITION not in HOLD_DISPOSITIONS
    assert REVIEW_CAPABILITY_UNMET_DISPOSITION not in DRAIN_DISPOSITIONS


def test_disposition_is_not_exported_from_the_package() -> None:
    from cw import dev_queue

    assert "REVIEW_CAPABILITY_UNMET_DISPOSITION" not in dev_queue.__all__
