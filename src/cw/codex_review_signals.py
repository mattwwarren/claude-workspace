"""Loud-failure signals for a codex review on a non-GitHub tracker (#3055).

RFC 0018 S3. The daemon's only tracker client is GitHub-only (ADR-0013), so a
codex review on any other tracker silently lost two things. This module turns
each into a durable event, plus the pure source classifier the fix loop
stamps on its terminal review:

* **Case A** -- :func:`emit_verdict_comment_skipped`: the verdict (or an
  unparseable result) was not posted as a ticket comment. Emitted from
  ``codex_review_persist._post_review_comment``'s skip branch.
* **Case B** -- :func:`undelivered_operator_context`: the review ran without
  the operator's tracker comments, voided findings and settle markers. Emitted
  once per session at run level (``codex_background``); the ``_context``
  loaders stay pure. ``requeue.review_delivery_degraded`` covers re-entry;
  this covers first entry.
* :func:`constraints_source`: where the fix loop's operator constraints came
  from (``Review.operator_constraints_source``).

**Best-effort (A15).** Case A runs after the session completed and Case B
before the review; an escaping exception would revert the row or abort the
review. Both emitters catch ``OSError`` from ``record_event`` (the inbox write
and lock path) only, log one warning, and return.

Leaf module: it must not import ``cw.codex_fix_loop`` or
``cw.codex_review_persist`` (``codex_fix_loop.baseline`` imports
``codex_review_persist``, which imports this module).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import OperatorConstraintsSource
from cw.events import record_event
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.tracker import TrackerClass, TrackerKind

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_log = logging.getLogger(__name__)

#: Which verdict comment a skip dropped: a parsed verdict or an unparseable one.
VerdictCommentKind = Literal["verdict", "unparseable"]

#: Case A's ``reason``: the only cause today is a non-GitHub tracker.
NON_GITHUB_TRACKER_REASON = "non_github_tracker"

#: The ``tracker`` label Case B carries when no tracker is configured.
UNSET_TRACKER_LABEL = "unset"

#: What a non-GitHub (or unset) tracker withholds from a codex review.
UNDELIVERED_OPERATOR_CONTEXT: tuple[str, ...] = (
    "operator_comments",
    "voided_findings",
    "settle_markers",
)


def constraints_source(
    *, has_comment: bool, row_entries: int, tracker: TrackerKind
) -> OperatorConstraintsSource:
    """Classify where the operator constraints reaching a codex review came from.

    A tracker comment and row entries give ``comment_and_row``; either alone
    gives ``comment`` or ``row``. Neither gives ``none`` on GitHub (nothing
    was set) and ``tracker_unreadable`` otherwise (comments could not be read,
    so their absence proves nothing). *row_entries* counts every
    ``task.operator_constraints`` entry, operator and plan-derived (A10).
    """
    has_row = row_entries > 0
    if has_comment and has_row:
        return OperatorConstraintsSource.COMMENT_AND_ROW
    if has_comment:
        return OperatorConstraintsSource.COMMENT
    if has_row:
        return OperatorConstraintsSource.ROW
    if tracker.tracker_class is TrackerClass.GITHUB:
        return OperatorConstraintsSource.NONE
    return OperatorConstraintsSource.TRACKER_UNREADABLE


def _record_best_effort(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> None:
    """``record_event``, swallowing ``OSError`` with one warning (A15)."""
    try:
        record_event(event_type, payload, correlation_id=ticket_id)
    except OSError as exc:
        _log.warning(
            "codex_review_signal_failed ticket=%s event=%s: %s",
            ticket_id,
            event_type.value,
            exc,
        )


def verdict_comment_skipped_payload(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
) -> dict[str, object]:
    """The ``review.verdict_comment_skipped`` payload, in documented order."""
    return {
        "ticket_id": ticket_id,
        "client": client,
        "tracker": tracker,
        "backend": CODEX_BACKEND,
        "kind": kind,
        "reason": NON_GITHUB_TRACKER_REASON,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "reviewed_sha": reviewed_sha or None,
        "session_id": session_id,
    }


def emit_verdict_comment_skipped(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
) -> None:
    """Case A: record that a codex verdict comment was not posted (best-effort)."""
    payload = verdict_comment_skipped_payload(
        ticket_id=ticket_id,
        client=client,
        tracker=tracker,
        kind=kind,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
    )
    _record_best_effort(
        OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED, payload, ticket_id
    )


def operator_context_undelivered_payload(
    *, task: TicketTask, client: str, tracker: TrackerKind, session_id: str
) -> dict[str, object]:
    """The ``review.operator_context_undelivered`` payload, in documented order."""
    row_constraints = len(task.operator_constraints)
    source = constraints_source(
        has_comment=False, row_entries=row_constraints, tracker=tracker
    )
    return {
        "ticket_id": task.ticket_id,
        "client": client,
        "tracker": tracker.system or UNSET_TRACKER_LABEL,
        "backend": CODEX_BACKEND,
        "undelivered": list(UNDELIVERED_OPERATOR_CONTEXT),
        "operator_constraints_source": source.value,
        "row_constraints": row_constraints,
        "row_dispositions": len(task.finding_dispositions),
        "session_id": session_id,
    }


def undelivered_operator_context(
    *, task: TicketTask, client: str, tracker: TrackerKind, session_id: str
) -> None:
    """Case B: record that a codex review ran without tracker context.

    A no-op on a GitHub tracker, where the context loaders read every
    comment. Best-effort: never raises ``OSError``.
    """
    if tracker.tracker_class is TrackerClass.GITHUB:
        return
    payload = operator_context_undelivered_payload(
        task=task, client=client, tracker=tracker, session_id=session_id
    )
    _record_best_effort(
        OrchestratorEventType.REVIEW_OPERATOR_CONTEXT_UNDELIVERED,
        payload,
        task.ticket_id,
    )
