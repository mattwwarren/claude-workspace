"""Loud-failure signals for a codex review on a non-GitHub tracker (#3055).

RFC 0018 S3. The daemon's only tracker client is GitHub-only (ADR-0013), so a
codex review on any other tracker silently lost two things. This module turns
each into a durable event, plus the pure source classifier the fix loop
stamps on its terminal review:

* **Case A** -- :func:`emit_verdict_comment_skipped`: the verdict (or an
  unparseable result) was not posted as a ticket comment. Emitted from
  ``codex_review_persist._post_review_comment``'s skip branch, and (#3057)
  when the tracker-IO turn refused to launch or the agent reported it could
  not post, with that turn's closed ``reason``.
* **Case A2** -- :func:`emit_verdict_comment_unverified` (#3057): the
  tracker-IO turn may have posted (timeout, crash, unparseable result) or did
  post but its self-attested report failed verification.
* **Case B** -- :func:`undelivered_operator_context`: the review ran without
  the operator's tracker comments, voided findings and settle markers. Emitted
  once per session at run level (``codex_background``); the ``_context``
  loaders stay pure. ``requeue.review_delivery_degraded`` covers re-entry;
  this covers first entry.
* :func:`constraints_source`: where the fix loop's operator constraints came
  from (``Review.operator_constraints_source``).
* :func:`emit_confirm_escalated` (#3216): ``review.confirm_escalated`` when a
  confirm round after a codex engine pass parks.

**Best-effort (A15).** Case A (and A2) runs after the session completed and Case B
before the review; an escaping exception would revert the row or abort the
review. Both emitters catch ``OSError`` from ``record_event`` (the inbox write
and lock path) only, log one warning, and return.

Leaf module: it must not import ``cw.codex_fix_loop`` or
``cw.codex_review_persist`` (``codex_fix_loop.baseline`` imports
``codex_review_persist``, which imports this module).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import OperatorConstraintsSource
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.tracker import TrackerClass, TrackerKind
from cw.tracker_io._verify import PublicState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from cw.models import TicketTask
    from cw.tracker_io import TrackerIoOutcome

_log = logging.getLogger(__name__)

# Shared by tracker-IO outcome handling and codex stage guards. This module is
# intentionally below both callers in the dependency graph: it contains no
# tracker-IO or stage-executor imports at runtime.
COMMON_TURN_FAULTS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    RuntimeError,
    LookupError,
    TypeError,
    AttributeError,
    subprocess.SubprocessError,
)

#: Which comment a skip dropped: a parsed verdict, an unparseable one, the
#: codex PLAN executor's plan or park comment (``plan``, #3060), or the codex
#: FINALIZE executor's PR-link comment (``pr_link``, #3062).
VerdictCommentKind = Literal["verdict", "unparseable", "plan", "pr_link"]

#: The log label each comment kind's skipped/unverified lines carry; a kind
#: absent here logs as ``review``.
_KIND_LOG_LABELS: dict[str, str] = {"plan": "plan", "pr_link": "pr_link"}


@dataclass(frozen=True)
class VerdictCommentIdentity:
    """Identity fields shared by tracker-IO verdict and PLAN comments."""

    client: str
    session_id: str
    kind: VerdictCommentKind
    reviewed_sha: str | None
    artifact_path: Path | None


#: Case A's default ``reason``: a non-GitHub tracker with no tracker-IO turn.
#: The turn's refusals carry its own closed ``cw.tracker_io.TrackerIoReason``
#: values instead (passed as plain strings: this module stays a leaf).
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


def record_event_best_effort(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> None:
    """``record_event``, swallowing event-write faults with one warning (A15)."""
    try:
        record_event(event_type, payload, correlation_id=ticket_id)
    except (OSError, CwError) as exc:
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
    reason: str = NON_GITHUB_TRACKER_REASON,
) -> dict[str, object]:
    """The ``review.verdict_comment_skipped`` payload, in documented order."""
    return {
        "ticket_id": ticket_id,
        "client": client,
        "tracker": tracker,
        "backend": CODEX_BACKEND,
        "kind": kind,
        "reason": reason,
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
    reason: str = NON_GITHUB_TRACKER_REASON,
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
        reason=reason,
    )
    record_event_best_effort(
        OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED, payload, ticket_id
    )


def verdict_comment_unverified_payload(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    reason: str,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    public_state: str,
    posted: Sequence[Mapping[str, object]],
    marker_survived: bool | None,
) -> dict[str, object]:
    """The ``review.verdict_comment_unverified`` payload, in documented order.

    The skipped payload's keys, then ``public_state``, ``posted`` (the
    agent-reported ``{index, comment_id, url}`` per part, model-echoed) and
    ``marker_survived``.
    """
    payload = verdict_comment_skipped_payload(
        ticket_id=ticket_id,
        client=client,
        tracker=tracker,
        kind=kind,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
        reason=reason,
    )
    payload["public_state"] = public_state
    payload["posted"] = [dict(entry) for entry in posted]
    payload["marker_survived"] = marker_survived
    return payload


def emit_verdict_comment_unverified(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    reason: str,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    public_state: str,
    posted: Sequence[Mapping[str, object]],
    marker_survived: bool | None,
) -> None:
    """Case A2: a tracker-IO verdict post is unconfirmed (best-effort)."""
    payload = verdict_comment_unverified_payload(
        ticket_id=ticket_id,
        client=client,
        tracker=tracker,
        kind=kind,
        reason=reason,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
        public_state=public_state,
        posted=posted,
        marker_survived=marker_survived,
    )
    record_event_best_effort(
        OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED, payload, ticket_id
    )


def verdict_comment_posted_payload(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    posted: Sequence[Mapping[str, object]],
    content: str,
) -> dict[str, object]:
    """The append-only audit payload for a verified tracker comment post."""
    return {
        "ticket_id": ticket_id,
        "client": client,
        "tracker": tracker,
        "backend": CODEX_BACKEND,
        "kind": kind,
        "posted": [dict(entry) for entry in posted],
        "reviewed_sha": reviewed_sha or None,
        "session_id": session_id,
        "posted_at": datetime.now(UTC).isoformat(),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
    }


def emit_verdict_comment_posted(
    *,
    ticket_id: str,
    client: str,
    tracker: str,
    kind: VerdictCommentKind,
    artifact_path: Path | None,
    reviewed_sha: str | None,
    session_id: str,
    posted: Sequence[Mapping[str, object]],
    content: str,
) -> None:
    """Record a verified tracker-IO post as an append-only audit event."""
    payload = verdict_comment_posted_payload(
        ticket_id=ticket_id,
        client=client,
        tracker=tracker,
        kind=kind,
        artifact_path=artifact_path,
        reviewed_sha=reviewed_sha,
        session_id=session_id,
        posted=posted,
        content=content,
    )
    record_event_best_effort(
        OrchestratorEventType.REVIEW_VERDICT_COMMENT_POSTED, payload, ticket_id
    )


def surface_tracker_io_outcome(
    ticket_id: str,
    content: str,
    *,
    tracker: str,
    outcome: TrackerIoOutcome,
    identity: VerdictCommentIdentity,
) -> None:
    """Classify one tracker-IO outcome and emit its verdict-comment event.

    Review verdicts and PLAN comments use the same A2 outcome contract; the
    identity carries the only stage-specific part, including ``kind``.
    """
    posted = [
        {"index": part.index, "comment_id": part.comment_id, "url": part.url}
        for part in outcome.posted
    ]
    if outcome.reason is None:
        emit_verdict_comment_posted(
            ticket_id=ticket_id,
            client=identity.client,
            tracker=tracker,
            kind=identity.kind,
            artifact_path=identity.artifact_path,
            reviewed_sha=identity.reviewed_sha,
            session_id=identity.session_id,
            posted=posted,
            content=content,
        )
        return
    label = _KIND_LOG_LABELS.get(identity.kind, "review")
    if outcome.public_state is PublicState.NONE:
        _log.warning(
            "%s_comment_skipped ticket=%s tracker=%s reason=%s: durable copy at %s",
            label,
            ticket_id,
            tracker,
            outcome.reason.value,
            identity.artifact_path,
        )
        emit_verdict_comment_skipped(
            ticket_id=ticket_id,
            client=identity.client,
            tracker=tracker,
            kind=identity.kind,
            artifact_path=identity.artifact_path,
            reviewed_sha=identity.reviewed_sha,
            session_id=identity.session_id,
            reason=outcome.reason.value,
        )
        return
    _log.warning(
        "%s_comment_unverified ticket=%s tracker=%s reason=%s public_state=%s"
        " ids=%s: check the ticket before any re-run",
        label,
        ticket_id,
        tracker,
        outcome.reason.value,
        outcome.public_state.value,
        ",".join(p.comment_id for p in outcome.posted),
    )
    emit_verdict_comment_unverified(
        ticket_id=ticket_id,
        client=identity.client,
        tracker=tracker,
        kind=identity.kind,
        reason=outcome.reason.value,
        artifact_path=identity.artifact_path,
        reviewed_sha=identity.reviewed_sha,
        session_id=identity.session_id,
        public_state=outcome.public_state.value,
        posted=posted,
        marker_survived=outcome.marker_survived,
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
    record_event_best_effort(
        OrchestratorEventType.REVIEW_OPERATOR_CONTEXT_UNDELIVERED,
        payload,
        task.ticket_id,
    )


def emit_confirm_escalated(payload: dict[str, object], *, ticket_id: str) -> None:
    """``review.confirm_escalated`` for a codex engine-confirm park (#3216).

    The codex peer of claude-native's ``announce_confirm_park``: same event and
    payload, best-effort (an ``OSError`` logs ``codex_review_signal_failed``).
    """
    record_event_best_effort(
        OrchestratorEventType.REVIEW_CONFIRM_ESCALATED, payload, ticket_id
    )
