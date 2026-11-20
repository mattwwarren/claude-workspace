"""Claim-time review-capability preflight (#3790).

A client whose ``review.engine`` lists ``requires_runtime_capabilities`` may
run a review on a lane only if that lane's REVIEW runner declares every listed
capability (``pipeline.executors.review.runtime_capabilities``). When the
assigned runner falls short, the claim screen parks the PENDING row with
``REVIEW_CAPABILITY_UNMET_DISPOSITION`` before anything is claimed.

The park is uncharged by construction: the row never leaves PENDING, so no
``attempts``, ``unproductive_attempts`` or ``regress_attempts`` is spent, the
stage stays REVIEW, and no spawn, attestation or ledger write happens. There is
no automatic lane reassignment; the operator fixes config or re-lanes with
``cw dev-queue move`` and then ``requeue``.

Pure config, no I/O, so it is safe under the dev-queue claim lock. The doctor
advisory reuses :func:`review_capability_gap` so the gate and the advisory
cannot drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.dev_queue import save_dev_queue, transition_task_status
from cw.dev_queue.dispositions import (
    _PRE_DISPATCH_REVIEW_CAPABILITY_REASON,
    REVIEW_CAPABILITY_UNMET_DISPOSITION,
)
from cw.dispatch.claim.events import _emit_review_capability_attention_event
from cw.executor import resolve_executor_config, resolve_pipeline_stages
from cw.models import QueueItemStatus, Stage, TicketTask

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore

_log = logging.getLogger("cw.dispatch")


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    """One lane's REVIEW runner short of a client's required capabilities."""

    lane: str
    backend: str
    required: tuple[str, ...]
    declared: tuple[str, ...]
    missing: tuple[str, ...]
    compatible_lanes: tuple[str, ...]


def _declared_on_lane(client: ClientConfig, lane_name: str) -> frozenset[str] | None:
    """Capabilities the lane's REVIEW runner declares, or ``None`` without REVIEW."""
    # ticket_id="" is the documented "no associated ticket" sentinel.
    task = TicketTask(
        ticket_id="", client=client.name, lane=lane_name, stage=Stage.REVIEW
    )
    if Stage.REVIEW not in resolve_pipeline_stages(task, client):
        return None
    runner = resolve_executor_config(Stage.REVIEW, task, client)
    return frozenset(runner.runtime_capabilities)


def _compatible_lanes(
    client: ClientConfig, lane_name: str, required: frozenset[str]
) -> tuple[str, ...]:
    """Other lanes whose REVIEW runner declares a superset of *required*."""
    compatible: list[str] = []
    for lane in client.effective_lanes:
        if lane.name == lane_name:
            continue
        declared = _declared_on_lane(client, lane.name)
        if declared is not None and required <= declared:
            compatible.append(lane.name)
    return tuple(sorted(compatible))


def review_capability_gap(
    task: TicketTask, client: ClientConfig
) -> CapabilityGap | None:
    """The capability shortfall for a REVIEW *task*, or ``None`` when it may run.

    ``None`` unless the task is at REVIEW and the client's engine requires a
    non-empty set. A runner declaring a superset of the required slugs is
    compatible.
    """
    engine = client.review.engine
    if task.stage is not Stage.REVIEW or engine is None:
        return None
    required = frozenset(engine.requires_runtime_capabilities)
    if not required:
        return None
    runner = resolve_executor_config(Stage.REVIEW, task, client)
    declared = frozenset(runner.runtime_capabilities)
    missing = required - declared
    if not missing:
        return None
    return CapabilityGap(
        lane=task.lane,
        backend=runner.backend,
        required=tuple(sorted(required)),
        declared=tuple(sorted(declared)),
        missing=tuple(sorted(missing)),
        compatible_lanes=_compatible_lanes(client, task.lane, required),
    )


def park_review_capability_unmet(
    task: TicketTask,
    client: ClientConfig,
    client_name: str,
    lane: str,
    store: DevQueueStore,
) -> bool:
    """Park *task* when its lane's REVIEW runner lacks a required capability.

    Returns ``True`` iff the row was parked. ``unproductive=False`` and a
    PENDING-to-BLOCKED transition: no RUNNING exit, so nothing is charged.
    """
    gap = review_capability_gap(task, client)
    if gap is None:
        return False
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=REVIEW_CAPABILITY_UNMET_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_REVIEW_CAPABILITY_REASON,
        # BLOCKED_ON_USER assigns pr_url unconditionally; carry the row's link.
        pr_url=task.pr_url,
        unproductive=False,
    )
    save_dev_queue(store)
    _log.warning(
        "dispatch: review-capability park %s/%s lane=%s missing=%s",
        client_name,
        task.ticket_id,
        gap.lane,
        ",".join(gap.missing),
    )
    _emit_review_capability_attention_event(task, client_name, lane, gap)
    return True
