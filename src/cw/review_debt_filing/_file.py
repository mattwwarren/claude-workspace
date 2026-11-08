"""File one review-debt ticket per ticketless deferral (#3041, RFC 0017 D-7).

:func:`file_review_debt` is the library entry point. Order, per R4/R6:

1. refuse when the row is unresolved (nothing is created without a place to
   record it) or the repo visibility is unknown (R8);
2. dedup against the row's records, with no ``gh`` call;
3. ensure the ``review-debt`` label once;
4. list the open ``review-debt`` issues once and dedup by body marker, scoped
   to this queue ticket (a truncated listing with no match fails closed);
5. create each remaining entry while the budget lasts.

It never raises on a ``gh`` failure and never takes a lock; the caller records
the returned records under ``dev_queue_lock`` afterwards. Filing is single
writer per queue ticket: the check-then-act above is unlocked on purpose,
because no ``gh`` call may run under a lock (ADR-0019).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.gh import GhFailure
from cw.models.review_debt_record import FILED_MODE, ReviewDebtRecord
from cw.review_debt_filing._constants import (
    CREATE_ISSUE_TIMEOUT_S,
    ENSURE_LABEL_TIMEOUT_S,
    LIST_ISSUES_TIMEOUT_S,
    MIN_CALL_TIMEOUT_S,
    REVIEW_DEBT_LABEL,
    REVIEW_DEBT_LABEL_COLOR,
    REVIEW_DEBT_LABEL_DESCRIPTION,
)
from cw.review_debt_filing._events import (
    emit_deduped,
    emit_filed,
    emit_filing_failed,
)
from cw.review_debt_filing._marker import body_matches
from cw.review_debt_filing._models import (
    FilingFailure,
    FilingFailureKind,
    FilingResult,
)
from cw.review_debt_filing._render import (
    redact_detail,
    render_body,
    render_title,
    stderr_summary,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.gh import LabelledIssuesLookup
    from cw.review_debt_filing._models import DebtEntry, FilingContext
    from cw.review_debt_filing._tracker import DebtTracker

_GH_KIND: dict[GhFailure, FilingFailureKind] = {
    GhFailure.NETWORK: FilingFailureKind.NETWORK,
    GhFailure.AUTH: FilingFailureKind.AUTH,
    GhFailure.LABEL: FilingFailureKind.LABEL,
    GhFailure.MILESTONE: FilingFailureKind.OTHER,
    GhFailure.OTHER: FilingFailureKind.OTHER,
}

_NO_ROW_DETAIL = "no dev-queue row for the queue ticket; nothing created"
_NO_VISIBILITY_DETAIL = "repo visibility could not be determined"
_BUDGET_DETAIL = "debt filing budget exhausted"
_TRUNCATED_DETAIL = "open review-debt listing hit its limit; dedup inconclusive"
_VIA_ROW = "row"
_VIA_MARKER = "marker"


def unticketed(entries: Sequence[DebtEntry]) -> list[DebtEntry]:
    """*entries* without a ticket, one per key (the first wins)."""
    seen: set[str] = set()
    out: list[DebtEntry] = []
    for entry in entries:
        if entry.ticket or entry.key in seen:
            continue
        seen.add(entry.key)
        out.append(entry)
    return out


class _Run:
    """One filing run's budget and accumulated outcome."""

    def __init__(self, ctx: FilingContext, tracker: DebtTracker) -> None:
        self.ctx = ctx
        self.tracker = tracker
        self.deadline = ctx.clock() + ctx.budget_s
        self.records: list[ReviewDebtRecord] = []
        self.failures: list[FilingFailure] = []
        self.deduped: list[str] = []

    def timeout(self, cap: int) -> int | None:
        """``min(cap, remaining)``, or None when the budget is spent."""
        remaining = self.deadline - self.ctx.clock()
        if remaining < MIN_CALL_TIMEOUT_S:
            return None
        return min(cap, int(remaining))

    def fail(self, entry: DebtEntry, kind: FilingFailureKind, detail: str) -> None:
        redacted = redact_detail(detail, self.ctx)
        summary = stderr_summary(entry, self.ctx)
        self.failures.append(FilingFailure(entry.key, kind, redacted, summary))
        emit_filing_failed(self.ctx, entry.key, kind.value, redacted)

    def fail_all(
        self, entries: Sequence[DebtEntry], kind: FilingFailureKind, detail: str
    ) -> None:
        for entry in entries:
            self.fail(entry, kind, detail)

    def dedupe(self, key: str, tracker_id: str, via: str) -> None:
        self.deduped.append(key)
        emit_deduped(self.ctx, key, tracker_id, via)

    def result(self) -> FilingResult:
        return FilingResult(self.records, self.failures, tuple(self.deduped))


def _drop_row_recorded(run: _Run, pending: list[DebtEntry]) -> list[DebtEntry]:
    """Entries the row has no ticketed record for; the rest are deduped."""
    recorded = {
        record.key: record.ticket_id
        for record in run.ctx.existing or ()
        if record.ticket_id is not None
    }
    remaining: list[DebtEntry] = []
    for entry in pending:
        ticket_id = recorded.get(entry.key)
        if ticket_id is None:
            remaining.append(entry)
        else:
            run.dedupe(entry.key, ticket_id, _VIA_ROW)
    return remaining


def _ensure_label(run: _Run, pending: list[DebtEntry]) -> bool:
    """Ensure the label once; on failure every pending entry fails."""
    timeout = run.timeout(ENSURE_LABEL_TIMEOUT_S)
    if timeout is None:
        run.fail_all(pending, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return False
    failure = run.tracker.ensure_label(
        REVIEW_DEBT_LABEL,
        REVIEW_DEBT_LABEL_COLOR,
        REVIEW_DEBT_LABEL_DESCRIPTION,
        timeout=timeout,
    )
    if failure is not None:
        detail = f"gh label create {REVIEW_DEBT_LABEL} failed ({failure.value})"
        run.fail_all(pending, _GH_KIND[failure], detail)
        return False
    return True


def _lookup(run: _Run, pending: list[DebtEntry]) -> LabelledIssuesLookup | None:
    """List the open debt issues once; on failure every pending entry fails."""
    timeout = run.timeout(LIST_ISSUES_TIMEOUT_S)
    if timeout is None:
        run.fail_all(pending, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return None
    lookup = run.tracker.list_open_issues_by_label(REVIEW_DEBT_LABEL, timeout=timeout)
    if lookup.failure is not None:
        detail = lookup.detail or f"gh issue list failed ({lookup.failure.value})"
        run.fail_all(pending, FilingFailureKind.LOOKUP_FAILED, detail)
        return None
    return lookup


def _recovered(run: _Run, key: str, number: int) -> ReviewDebtRecord:
    """A ``filed`` record for an issue found by its body marker.

    The listing carries no URL, so ``ticket_url`` is the ``#<n>`` reference.
    """
    return ReviewDebtRecord(
        key=key,
        tracker=run.ctx.tracker,
        ticket_id=str(number),
        ticket_url=f"#{number}",
        filed_at=run.ctx.now(),
        mode=FILED_MODE,
    )


def _file_one(run: _Run, entry: DebtEntry, lookup: LabelledIssuesLookup) -> None:
    """Dedup *entry* by marker, else create it while the budget lasts."""
    for issue in lookup.issues:
        if body_matches(issue.body, entry.key, run.ctx.ticket_ref):
            run.records.append(_recovered(run, entry.key, issue.number))
            run.dedupe(entry.key, str(issue.number), _VIA_MARKER)
            return
    if lookup.truncated:
        run.fail(entry, FilingFailureKind.LOOKUP_FAILED, _TRUNCATED_DETAIL)
        return
    timeout = run.timeout(CREATE_ISSUE_TIMEOUT_S)
    if timeout is None:
        run.fail(entry, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return
    created = run.tracker.create_issue(
        render_title(entry, run.ctx),
        render_body(entry, run.ctx),
        labels=[REVIEW_DEBT_LABEL],
        timeout=timeout,
    )
    if created.number is None or created.url is None:
        kind = _GH_KIND[created.failure or GhFailure.OTHER]
        run.fail(entry, kind, created.detail or "gh issue create failed")
        return
    record = ReviewDebtRecord(
        key=entry.key,
        tracker=run.ctx.tracker,
        ticket_id=str(created.number),
        ticket_url=created.url,
        filed_at=run.ctx.now(),
        mode=FILED_MODE,
    )
    run.records.append(record)
    emit_filed(run.ctx, entry.key, str(created.number), created.url)


def file_review_debt(
    entries: Sequence[DebtEntry], ctx: FilingContext, tracker: DebtTracker
) -> FilingResult:
    """File every ticketless entry in *entries* on a GitHub-tracked client.

    See the module docstring for the order. Returns the records to upsert,
    the per-entry failures and the deduplicated keys.
    """
    run = _Run(ctx, tracker)
    pending = unticketed(entries)
    if not pending:
        return run.result()
    if ctx.existing is None:
        run.fail_all(pending, FilingFailureKind.OTHER, _NO_ROW_DETAIL)
        return run.result()
    if ctx.visibility is None:
        run.fail_all(
            pending, FilingFailureKind.VISIBILITY_UNDETERMINED, _NO_VISIBILITY_DETAIL
        )
        return run.result()
    pending = _drop_row_recorded(run, pending)
    if not pending or not _ensure_label(run, pending):
        return run.result()
    lookup = _lookup(run, pending)
    if lookup is None:
        return run.result()
    for entry in pending:
        _file_one(run, entry, lookup)
    return run.result()
