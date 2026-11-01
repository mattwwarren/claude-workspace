"""Assembly for :mod:`cw.review_history`: the pure build (#2790).

``build_review_history`` turns what ``collect_history_inputs`` read into a
:class:`~cw.models.review_history.ReviewHistory` with no I/O at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import QueueItemStatus, Stage
from cw.models.review_history import (
    DispositionView,
    HistorySource,
    ReviewHistory,
)
from cw.review_convergence import (
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_IN_PROGRESS,
)
from cw.review_finding_dispositions import split_disposition_key
from cw.review_history.attribution import (
    _attribute_voided,
    _attributed_stage_start,
    _parse_iso,
    _with_wall_times,
)
from cw.review_history.inputs import HistoryInputs, _int, _Part, _review_block
from cw.review_history.ledger import _ledger_part
from cw.review_history.sentinel import _sentinel_part

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import TicketTask
    from cw.review_convergence import ConvergenceLedger

_IN_FLIGHT_STATUSES = frozenset(
    {LEDGER_STATUS_IN_PROGRESS, LEDGER_STATUS_AWAITING_CONFIRM}
)


def build_review_history(inputs: HistoryInputs) -> ReviewHistory:
    """Turn *inputs* into the documented view; pure, never raises on bad data."""
    task = inputs.task
    notes = list(inputs.notes)
    review = _review_block(inputs.sentinel)
    if inputs.ledger is not None:
        part = _ledger_part(inputs.ledger, inputs, notes)
        _cross_check(inputs.ledger, review, notes)
    else:
        part = _sentinel_part(inputs.sentinel, review, notes)
    round_one = part.rounds[0] if part.rounds else None
    round_one_at = (
        _parse_iso(round_one.recorded_at)
        if round_one is not None and round_one.source == "ledger"
        else None
    )
    start = _attributed_stage_start(inputs, round_one_at, notes)
    if round_one_at is not None and start is None:
        notes.append(
            "round 1 wall time not recorded: no s3_review_started event from "
            "this row's sessions precedes it"
        )
    voided = _attribute_voided(inputs, notes)
    return ReviewHistory(
        ticket_id=task.ticket_id,
        client=task.client,
        row_status=task.status.value,
        row_stage=task.stage.value,
        in_flight=_in_flight(inputs),
        rounds=_with_wall_times(part.rounds, start),
        summary=part.summary,
        settled=part.settled,
        dispositions=_disposition_views(task),
        voided=voided,
        sources=_sources(
            inputs, part, review, events_used=start is not None or bool(voided)
        ),
        notes=notes,
    )


def _in_flight(inputs: HistoryInputs) -> bool:
    if inputs.ledger is not None:
        return inputs.ledger.status in _IN_FLIGHT_STATUSES
    task = inputs.task
    return task.status == QueueItemStatus.RUNNING and task.stage == Stage.REVIEW


def _sources(
    inputs: HistoryInputs,
    part: _Part,
    review: Mapping[str, object] | None,
    *,
    events_used: bool,
) -> list[HistorySource]:
    sources: list[HistorySource] = []
    if inputs.ledger is not None:
        sources.append("ledger")
        if any(r.new_by_severity is not None for r in part.rounds):
            sources.append("verdict_files")
    elif review is not None or part.rounds:
        sources.append("sentinel")
    if inputs.task.finding_dispositions:
        sources.append("row_dispositions")
    if events_used:
        sources.append("events")
    return sources


def _cross_check(
    ledger: ConvergenceLedger, review: Mapping[str, object] | None, notes: list[str]
) -> None:
    rounds_run = None if review is None else _int(review.get("rounds_run"))
    if rounds_run is not None and rounds_run != len(ledger.rounds):
        notes.append(
            f"sentinel rounds_run={rounds_run} disagrees with the ledger's "
            f"{len(ledger.rounds)} round(s); the ledger wins"
        )


def _disposition_views(task: TicketTask) -> list[DispositionView]:
    return [
        DispositionView(
            key=key,
            file=split_disposition_key(key)[0],
            summary=entry.summary,
            outcome=entry.outcome,
            rationale=entry.rationale,
            actor=entry.actor,
            recorded_at=entry.recorded_at,
            reviewed_sha=entry.reviewed_sha,
        )
        for key, entry in sorted(task.finding_dispositions.items())
    ]
