"""Link filed review-debt tickets to the PR that carried them (#3041, R11).

For each ``filed`` GitHub record with no ``pr_url``: append the
``<!-- cw-review-debt-pr: <url> -->`` line to the ticket body (III2, #3044,
parses it), post one comment linking the PR, then set ``pr_url``. Idempotent:
a body that already carries the marker gets no edit and no comment, and only
its ``pr_url`` is set. If the body edit lands but the comment fails, the next
run sees the marker and sets ``pr_url`` without commenting. Failures degrade:
they are counted, never raised, and never block finalize (plan Adopted
Assumption 12), because the ticket already exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.models.review_debt_record import FILED_MODE
from cw.review_debt_filing._constants import (
    FETCH_BODY_TIMEOUT_S,
    KEY_DISPLAY_CHARS,
    POST_COMMENT_TIMEOUT_S,
    TRACKER_GITHUB,
    UPDATE_BODY_TIMEOUT_S,
)
from cw.review_debt_filing._marker import has_pr_marker, with_pr_marker
from cw.review_debt_filing._models import LinkOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_debt_record import ReviewDebtRecord
    from cw.review_debt_filing._tracker import DebtTracker


class LinkResult(NamedTuple):
    """What :func:`link_pr` did; ``records`` carry the newly set ``pr_url``."""

    records: list[ReviewDebtRecord]
    linked: int
    already_linked: int
    failed: int
    skipped: int


def linkable(record: ReviewDebtRecord) -> bool:
    """True for a ``filed`` GitHub record; ``emitted``/Linear ones are skipped."""
    return (
        record.mode == FILED_MODE
        and record.tracker == TRACKER_GITHUB
        and record.ticket_id is not None
        and record.ticket_id.isdigit()
    )


def _comment(record: ReviewDebtRecord, pr_url: str) -> str:
    return (
        f"Review debt linked to {pr_url} "
        f"(cw review-debt key `{record.key[:KEY_DISPLAY_CHARS]}`)."
    )


def _link_one(
    record: ReviewDebtRecord, pr_url: str, tracker: DebtTracker
) -> LinkOutcome:
    """Return the typed outcome for one linkable record."""
    ticket_id = str(record.ticket_id)
    body = tracker.fetch_issue_body(ticket_id, timeout=FETCH_BODY_TIMEOUT_S)
    if body is None:
        return LinkOutcome.FAILED
    if has_pr_marker(body, pr_url):
        return LinkOutcome.ALREADY
    updated = tracker.update_issue_body(
        int(ticket_id), with_pr_marker(body, pr_url), timeout=UPDATE_BODY_TIMEOUT_S
    )
    if not updated:
        return LinkOutcome.FAILED
    posted = tracker.post_issue_comment(
        ticket_id, _comment(record, pr_url), timeout=POST_COMMENT_TIMEOUT_S
    )
    return LinkOutcome.LINKED if posted else LinkOutcome.FAILED


def link_pr(
    records: Sequence[ReviewDebtRecord], pr_url: str, tracker: DebtTracker
) -> LinkResult:
    """Link every linkable record in *records* without a ``pr_url`` to *pr_url*."""
    updated: list[ReviewDebtRecord] = []
    counts = dict.fromkeys(LinkOutcome, 0)
    for record in records:
        if not linkable(record):
            counts[LinkOutcome.SKIPPED] += 1
            continue
        if record.pr_url is not None:
            counts[LinkOutcome.ALREADY] += 1
            continue
        outcome = _link_one(record, pr_url, tracker)
        counts[outcome] += 1
        if outcome is not LinkOutcome.FAILED:
            updated.append(record.model_copy(update={"pr_url": pr_url}))
    return LinkResult(
        updated,
        counts[LinkOutcome.LINKED],
        counts[LinkOutcome.ALREADY],
        counts[LinkOutcome.FAILED],
        counts[LinkOutcome.SKIPPED],
    )
