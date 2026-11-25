"""Approval evidence for the codex PLAN lane's resumed rounds (#3465).

Pure functions over the tracker observation, the row and the resumed draft.
Two routes can approve a large plan without a row approval that matches the
draft's fingerprint, and both apply only to resumed rounds:

- the operator-authority alternate (:func:`alternate_holds`): the row approval
  still stands because no operator-authority comment is newer than it and the
  persisted ``body_sha`` equals the live body's;
- a comment token (:func:`approving_comment`): an operator-identity comment
  carrying :data:`APPROVAL_LINE` directly after a park header, consumed as soon as the
  plan changes (see :class:`ApprovalFacts`).

Neither route is a fast path: a scan turn still runs, and a token approves only
while the plan's fingerprint still equals the one the previous park persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.codex_plan._result import draft_body
from cw.plan_files import _strip_fenced_lines
from cw.plan_fingerprint import (
    compute_plan_draft_fingerprint,
    parse_last_evaluated_body_sha,
)
from cw.stage_verification.plan_gates import CommentApproval

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.codex_plan._result import PlanSettlement, ResumeDraft
    from cw.codex_plan._tracker import AuthorityComment, TrackerObservation
    from cw.models import TicketTask

#: The approval comment token an operator posts on a parked plan (exact line).
APPROVAL_LINE: Final = "<!-- auto-dev-comment-approval -->"


@dataclass(frozen=True)
class ApprovalFacts:
    """What the approval routes established for one resumed round.

    ``alternate`` is the operator-authority delta route; ``comment_id`` the
    approving token comment; ``parked_fingerprint`` the fingerprint the previous
    park persisted on disk, which a comment approval must still match.
    """

    alternate: bool = False
    comment_id: str | None = None
    parked_fingerprint: str | None = None

    def comment_approval(self, plan_text: str) -> CommentApproval | None:
        """The comment approval compared against *plan_text*, ``None`` without one."""
        if self.comment_id is None or self.parked_fingerprint is None:
            return None
        return CommentApproval(
            comment_id=self.comment_id,
            parked=self.parked_fingerprint,
            current=compute_plan_draft_fingerprint(draft_body(plan_text)),
        )


NO_APPROVAL: Final = ApprovalFacts()


def has_approval_token(body: str) -> bool:
    """True when a line, outside fenced code, is exactly :data:`APPROVAL_LINE`."""
    return any(
        line.strip() == APPROVAL_LINE for line in _strip_fenced_lines(body.splitlines())
    )


def _parse_time(value: object) -> datetime | None:
    """An ISO-8601 instant, naive values read as UTC; ``None`` when unparsable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def no_authority_delta(
    approved_at: datetime | None, observation: TrackerObservation
) -> bool:
    """True when no operator-authority comment is newer than *approved_at*.

    Strictly newer counts; an unparsable comment time counts as newer (fail
    closed). An unobserved snapshot or a missing approval time gives False.
    """
    if not observation.observed or approved_at is None:
        return False
    cutoff = (
        approved_at
        if approved_at.tzinfo is not None
        else approved_at.replace(tzinfo=UTC)
    )
    for comment in observation.comments:
        created = _parse_time(comment.get("createdAt"))
        if created is None or created > cutoff:
            return False
    return True


def persisted_body_sha(last_evaluated: str | None) -> str | None:
    """The ``body_sha`` component of a persisted ``last_evaluated`` value."""
    if last_evaluated is None:
        return None
    return parse_last_evaluated_body_sha(last_evaluated)


def alternate_holds(
    task: TicketTask,
    observation: TrackerObservation,
    last_evaluated: str | None,
    *,
    row_delta: bool = False,
) -> bool:
    """The operator-authority alternate: a row approval that still stands.

    Requires a row approval (time and fingerprint), an observed snapshot, a
    persisted ``body_sha`` equal to the live body's, and no newer operator
    comment (a ``--post-marker`` audit comment counts as one). A row delta
    never takes the alternate: a delivered revision keeps exact equality.
    """
    if row_delta or task.plan_approved_at is None or not task.plan_approved_fingerprint:
        return False
    if not observation.observed:
        return False
    if persisted_body_sha(last_evaluated) != observation.body_sha:
        return False
    return no_authority_delta(task.plan_approved_at, observation)


def is_stale(stream: Sequence[AuthorityComment], comment_id: str) -> bool:
    """True when an operator comment or a pipeline header follows *comment_id*.

    An unknown id is stale (fail closed). Agent comments without a header are
    not in the stream and never make an approval stale.
    """
    index = next((i for i, entry in enumerate(stream) if entry.id == comment_id), None)
    if index is None:
        return True
    return any(entry.operator or entry.header for entry in stream[index + 1 :])


def follows_park(stream: Sequence[AuthorityComment], comment_id: str) -> bool:
    """True when the nearest header before *comment_id* is a park header."""
    index = next((i for i, entry in enumerate(stream) if entry.id == comment_id), None)
    if index is None:
        return False
    for entry in reversed(stream[:index]):
        if entry.header:
            return entry.park
    return False


def is_double_duty(
    comment_id: str, settled_sources: Sequence[str], resolutions_source: str | None
) -> bool:
    """True when the comment also settles items or carries the resolutions marker."""
    return (
        comment_id in settled_sources or resolutions_source == f"comment:{comment_id}"
    )


def approving_comment(
    observation: TrackerObservation,
    *,
    settled_sources: Sequence[str],
    resolutions_source: str | None,
) -> str | None:
    """The id of the comment that approves, or ``None``.

    The approving comment is the latest approver-identity comment carrying the
    token. It must follow a park, be the last operator comment (not stale), and
    not also settle items or carry resolutions.
    """
    if not observation.observed:
        return None
    stream = observation.authority_stream
    token_entries = [e for e in stream if e.approver and has_approval_token(e.body)]
    if not token_entries:
        return None
    candidate = token_entries[-1].id
    if is_stale(stream, candidate) or not follows_park(stream, candidate):
        return None
    if is_double_duty(candidate, settled_sources, resolutions_source):
        return None
    return candidate


def approval_facts(
    task: TicketTask,
    observation: TrackerObservation,
    last_evaluated: str | None,
    *,
    row_delta: bool,
    settlements: Sequence[PlanSettlement],
    resolutions_source: str | None,
    resume: ResumeDraft | None,
) -> ApprovalFacts:
    """The facts for one resumed round; :data:`NO_APPROVAL` on a fresh run.

    Called before the round's fingerprint settle overwrites *last_evaluated*.
    """
    if resume is None:
        return NO_APPROVAL
    comment_id = approving_comment(
        observation,
        settled_sources=[settlement.source for settlement in settlements],
        resolutions_source=resolutions_source,
    )
    return ApprovalFacts(
        alternate=alternate_holds(
            task, observation, last_evaluated, row_delta=row_delta
        ),
        comment_id=comment_id,
        parked_fingerprint=resume.fingerprint,
    )


__all__ = [
    "APPROVAL_LINE",
    "NO_APPROVAL",
    "ApprovalFacts",
    "alternate_holds",
    "approval_facts",
    "approving_comment",
    "follows_park",
    "has_approval_token",
    "is_double_duty",
    "is_stale",
    "no_authority_delta",
    "persisted_body_sha",
]
