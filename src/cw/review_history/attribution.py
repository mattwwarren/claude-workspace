"""Event attribution and wall time for :mod:`cw.review_history` (#2790).

Pure. A ticket id is unique per client, not globally, and the event inbox is
global, so an event is used only when it can be tied to this row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.models.review_history import RoundView, VoidedView
from cw.review_adjudication import VOIDED_RATIONALE_PREFIX
from cw.review_convergence import finding_identity
from cw.review_finding_dispositions import split_disposition_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import OrchestratorEvent, TicketTask
    from cw.review_convergence import ConvergenceLedger
    from cw.review_history.inputs import HistoryInputs


class _Match(NamedTuple):
    """The row record a voided event matched; severity is None for a disposition."""

    severity: str | None
    file: str
    summary: str
    rationale: str


# --- wall time and stage.entered attribution ----------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    """*value* as an aware datetime (naive reads as UTC), or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _attributed_stage_start(
    inputs: HistoryInputs, round_one_at: datetime | None, notes: list[str]
) -> datetime | None:
    """Latest attributable review start at or before round 1; counts the rest."""
    attributable: list[datetime] = []
    foreign = 0
    for event in inputs.stage_events:
        session_id = event.payload.get("session_id")
        if (
            isinstance(session_id, str)
            and session_id
            and session_id in inputs.bound_session_ids
        ):
            attributable.append(_aware(event.created_at))
        else:
            foreign += 1
    if foreign:
        notes.append(
            f"{foreign} stage.entered event(s) for ticket id "
            f"'{inputs.task.ticket_id}' could not be tied to this row's "
            "sessions; not used"
        )
    if round_one_at is None:
        return None
    starts = [moment for moment in attributable if moment <= round_one_at]
    return max(starts) if starts else None


def _with_wall_times(
    rounds: list[RoundView], start: datetime | None
) -> list[RoundView]:
    timed: list[RoundView] = []
    previous = start
    for view in rounds:
        current = _parse_iso(view.recorded_at)
        wall = (
            (current - previous).total_seconds()
            if current is not None and previous is not None
            else None
        )
        timed.append(view.model_copy(update={"wall_seconds": wall}))
        previous = current
    return timed


# --- review.finding_voided attribution --------------------------------------------


def _normalized_summary(summary: str) -> str:
    """*summary* under the producer's own rule, via the public finding identity."""
    # Why: a disposition entry has no severity; only summary_normalized is read.
    return finding_identity("SHOULD_FIX", "", summary).summary_normalized


def _payload_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _attribute_voided(inputs: HistoryInputs, notes: list[str]) -> list[VoidedView]:
    views: list[VoidedView] = []
    for event in inputs.voided_events:
        view = _voided_view(event, inputs)
        if view is not None:
            views.append(view)
    foreign = len(inputs.voided_events) - len(views)
    if foreign:
        notes.append(
            f"{foreign} review.finding_voided event(s) for ticket id "
            f"'{inputs.task.ticket_id}' could not be tied to this row; not shown"
        )
    return views


def _voided_view(event: OrchestratorEvent, inputs: HistoryInputs) -> VoidedView | None:
    """The view of *event* when it ties to one of this row's records, else None."""
    payload = event.payload
    severity = _payload_str(payload, "severity")
    file = _payload_str(payload, "file")
    summary = _payload_str(payload, "summary")
    comment = _payload_str(payload, "operator_comment_id")
    if not (severity and file and summary and comment):
        return None
    match = _ledger_match(inputs.ledger, severity, file, summary) or _disposition_match(
        inputs.task, file, summary
    )
    if match is None:
        return None
    if match.rationale.startswith(VOIDED_RATIONALE_PREFIX) and (
        comment not in match.rationale
    ):
        return None
    return VoidedView(
        severity=match.severity or severity,
        file=match.file,
        summary=match.summary,
        original_rationale=_payload_str(payload, "original_rationale"),
        voided_at=_payload_str(payload, "voided_at"),
        operator_comment_id=comment,
        event_at=_aware(event.created_at).isoformat(),
    )


def _ledger_match(
    ledger: ConvergenceLedger | None, severity: str, file: str, summary: str
) -> _Match | None:
    if ledger is None:
        return None
    for entry in ledger.settled:
        identity = entry.identity
        if identity.severity == severity and identity == finding_identity(
            identity.severity, file, summary
        ):
            return _Match(
                identity.severity,
                identity.file,
                identity.summary_normalized,
                entry.rationale,
            )
    return None


def _disposition_match(task: TicketTask, file: str, summary: str) -> _Match | None:
    wanted = _normalized_summary(summary)
    for key, entry in sorted(task.finding_dispositions.items()):
        if (
            entry.summary
            and split_disposition_key(key)[0] == file
            and _normalized_summary(entry.summary) == wanted
        ):
            return _Match(None, file, entry.summary, entry.rationale)
    return None
