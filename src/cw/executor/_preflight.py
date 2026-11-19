"""The executors' call into the generic stage pre-flight (RFC 0019 H2, #3709).

:func:`park_if_unmet` builds the spawn's :class:`~cw.stage_preflight.SpawnFacts`
from the row and the client, asks :func:`~cw.stage_preflight.first_unmet`, and
words a park with the executor's own blocked-result constructor
(``local_runner.make_blocked``, ``opencode_runner.make_blocked`` or
``codex_review.make_codex_blocked``). It replaces the per-backend guard modules
and runs at their old park positions: after claim and before any binary or
endpoint probe, so attempt charging and the persisted shape stay as they were.

It lives in the executor package, not in the leaf, because the engine refusal
text comes from ``cw.review_engine``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from cw.models.repair_entry import is_adopted_repair_entry
from cw.opencode_runner import stage_entry_marker
from cw.review_engine.constants import (
    CAUSE_BACKEND_UNSUPPORTED,
    engine_refusal_details,
)
from cw.stage_preflight import SpawnFacts, first_unmet

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, StageReached
    from cw.models import ClientConfig, Stage, TicketTask
    from cw.models.capabilities import HarnessCapabilities


class BlockedCtor(Protocol):
    """The keyword surface the three runners' blocked constructors share."""

    def __call__(
        self,
        *,
        ticket_id: str,
        worktree: Path,
        reason: str,
        details: str = ...,
        retry_eligible: bool | None = ...,
        stage_reached: StageReached = ...,
    ) -> AutoDevResult: ...


def spawn_facts(
    caps: HarnessCapabilities, *, task: TicketTask, client: ClientConfig, stage: Stage
) -> SpawnFacts:
    """The facts :func:`first_unmet` checks for this spawn on *caps*'s harness."""
    engine = client.review.engine
    engine_refusal = (
        None
        if engine is None
        else engine_refusal_details(
            caps.harness.value, CAUSE_BACKEND_UNSUPPORTED, f"skill={engine.skill}"
        )
    )
    return SpawnFacts(
        stage=stage,
        clean_review=client.review.is_clean,
        engine_refusal=engine_refusal,
        feedback=task.review_feedback is not None,
        adopted=is_adopted_repair_entry(task),
    )


def park_if_unmet(
    caps: HarnessCapabilities,
    *,
    task: TicketTask,
    client: ClientConfig,
    stage: Stage,
    worktree: Path,
    blocked: BlockedCtor,
) -> AutoDevResult | None:
    """The blocked result for the first unmet need, or ``None`` to carry on.

    Every park carries the dispatched stage's entry marker, so dispatch never
    walks ``task.stage`` forward on it.
    """
    unmet = first_unmet(caps, spawn_facts(caps, task=task, client=client, stage=stage))
    if unmet is None:
        return None
    return blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=unmet.reason.value,
        details=unmet.details,
        retry_eligible=unmet.retry_eligible,
        stage_reached=stage_entry_marker(stage.value),
    )
