"""File review-debt findings into one roll-up issue per queue ticket (#3041).

:func:`file_review_debt` is the library entry point. Order, per R4/R6:

1. refuse when the row is unresolved (nothing is created without a place to
   record it) or the repo visibility is unknown (R8);
2. dedup against the row's records: a recorded ticket counts only while it
   is still OPEN (one ``gh issue view --json state`` per distinct ticket,
   #3044). A CLOSED ticket is a miss, so the entry is filed again; an unknown
   state, a non-numeric ticket id or a spent budget count as a hit (no double
   file);
3. ensure the ``review-debt`` label once;
4. list the open ``review-debt`` issues once and dedup by body marker, scoped
   to this queue ticket. A key is found in a legacy per-finding issue and in a
   roll-up item by the same match; a truncated listing with no match fails
   closed;
5. list every remaining entry in the queue ticket's roll-up: append to its
   newest open part, and continue in a numbered continuation issue when the
   body would pass :data:`ROLLUP_BODY_MAX_CHARS` (see ``_rollup``). With no open
   roll-up the first issue is created. Legacy issues are never edited.

It never raises on a ``gh`` failure and never takes a lock; the caller records
the returned records under ``dev_queue_lock`` afterwards. Filing is single
writer per queue ticket: the check-then-act above is unlocked on purpose,
because no ``gh`` call may run under a lock (ADR-0019).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.gh import GhFailure
from cw.models.review_debt_record import FILED_MODE, ReviewDebtMode, ReviewDebtRecord
from cw.review_debt_filing._constants import (
    CREATE_ISSUE_TIMEOUT_S,
    ENSURE_LABEL_TIMEOUT_S,
    FETCH_BODY_TIMEOUT_S,
    GH_ISSUE_STATE_CLOSED,
    ISSUE_STATE_TIMEOUT_S,
    LIST_ISSUES_TIMEOUT_S,
    REVIEW_DEBT_LABEL,
    REVIEW_DEBT_LABEL_COLOR,
    REVIEW_DEBT_LABEL_DESCRIPTION,
    UPDATE_BODY_TIMEOUT_S,
)
from cw.review_debt_filing._events import (
    emit_deduped,
    emit_filed,
    emit_filing_failed,
)
from cw.review_debt_filing._marker import body_matches
from cw.review_debt_filing._models import (
    CallBudget,
    FilingFailure,
    FilingFailureKind,
    FilingResult,
)
from cw.review_debt_filing._render import redact_detail, stderr_summary
from cw.review_debt_filing._rollup import (
    RollupGroup,
    RollupItem,
    append_items,
    group_title,
    plan_groups,
    render_body,
    render_item,
    scoped_rollup,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.gh import LabelledIssue, LabelledIssuesLookup
    from cw.review_debt_filing._events import AuditWarning
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
_FETCH_BODY_DETAIL = "gh issue view failed while appending to the roll-up"
_UPDATE_BODY_DETAIL = "gh issue edit failed while appending to the roll-up"


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
        self.budget = CallBudget(ctx.clock, ctx.budget_s)
        self.records: list[ReviewDebtRecord] = []
        self.failures: list[FilingFailure] = []
        self.deduped: list[str] = []
        self.audit_warnings: list[AuditWarning] = []
        self.open_by_id: dict[str, bool] = {}

    def timeout(self, cap: int) -> int | None:
        """``min(cap, remaining)``, or None when the budget is spent."""
        return self.budget.timeout(cap)

    def fail(self, entry: DebtEntry, kind: FilingFailureKind, detail: str) -> None:
        redacted = redact_detail(detail, self.ctx)
        summary = stderr_summary(entry, self.ctx)
        self.failures.append(FilingFailure(entry.key, kind, redacted, summary))
        warning = emit_filing_failed(self.ctx, entry.key, kind.value, redacted)
        if warning is not None:
            self.audit_warnings.append(warning)

    def fail_all(
        self, entries: Sequence[DebtEntry], kind: FilingFailureKind, detail: str
    ) -> None:
        for entry in entries:
            self.fail(entry, kind, detail)

    def dedupe(self, key: str, tracker_id: str, via: str) -> None:
        self.deduped.append(key)
        warning = emit_deduped(self.ctx, key, tracker_id, via)
        if warning is not None:
            self.audit_warnings.append(warning)

    def result(self) -> FilingResult:
        return FilingResult(
            self.records,
            self.failures,
            tuple(self.deduped),
            tuple(self.audit_warnings),
        )


def _row_ticket_open(run: _Run, ticket_id: str) -> bool:
    """False only when *ticket_id* is known to be CLOSED (#3044, R6).

    Fail safe toward a hit: a non-numeric id, a spent budget or an unknown
    state all count as open, so a ``gh`` failure never files a duplicate. The
    answer is read once per ticket id: a roll-up's many records share one.
    """
    if not (ticket_id.isascii() and ticket_id.isdigit()):
        return True
    if ticket_id in run.open_by_id:
        return run.open_by_id[ticket_id]
    timeout = run.timeout(ISSUE_STATE_TIMEOUT_S)
    if timeout is None:
        return True
    state = run.tracker.fetch_issue_state(ticket_id, timeout=timeout)
    run.open_by_id[ticket_id] = state != GH_ISSUE_STATE_CLOSED
    return run.open_by_id[ticket_id]


def _drop_row_recorded(run: _Run, pending: list[DebtEntry]) -> list[DebtEntry]:
    """Entries with no open recorded ticket on the row; the rest are deduped."""
    recorded = {
        record.key: record.ticket_id
        for record in run.ctx.existing or ()
        if record.ticket_id is not None
    }
    remaining: list[DebtEntry] = []
    for entry in pending:
        ticket_id = recorded.get(entry.key)
        if ticket_id is not None and _row_ticket_open(run, ticket_id):
            run.dedupe(entry.key, ticket_id, _VIA_ROW)
        else:
            remaining.append(entry)
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


def _known_url(run: _Run, number: int) -> str:
    """The URL a row record already holds for issue *number*, else ``#<n>``.

    The listing carries no URL, so an issue found by marker or appended to is
    named by a sibling record's URL when the row has one.
    """
    for record in run.ctx.existing or ():
        if record.ticket_id == str(number) and record.ticket_url:
            return record.ticket_url
    return f"#{number}"


def _filed_record(
    run: _Run, key: str, number: int, url: str, mode: ReviewDebtMode = FILED_MODE
) -> ReviewDebtRecord:
    return ReviewDebtRecord(
        key=key,
        tracker=run.ctx.tracker,
        ticket_id=str(number),
        ticket_url=url,
        filed_at=run.ctx.now(),
        mode=mode,
    )


def _marker_hit(run: _Run, entry: DebtEntry, lookup: LabelledIssuesLookup) -> bool:
    """Dedup *entry* by body marker against a legacy issue or a roll-up item."""
    for issue in lookup.issues:
        if body_matches(issue.body, entry.key, run.ctx.ticket_ref):
            run.records.append(
                _filed_record(
                    run, entry.key, issue.number, _known_url(run, issue.number)
                )
            )
            run.dedupe(entry.key, str(issue.number), _VIA_MARKER)
            return True
    return False


def _target(run: _Run, lookup: LabelledIssuesLookup) -> tuple[int, int, str] | None:
    """``(number, part, body)`` of the newest open roll-up for the queue ticket."""
    best: tuple[int, LabelledIssue] | None = None
    for issue in lookup.issues:
        part = scoped_rollup(issue.body, run.ctx.ticket_ref)
        if part is not None and (best is None or part > best[0]):
            best = (part, issue)
    if best is None:
        return None
    return best[1].number, best[0], best[1].body


def _record_listed(run: _Run, group: RollupGroup, number: int, url: str) -> None:
    """Record, and audit, every finding *group* listed in issue *number*."""
    for item in group.items:
        run.records.append(_filed_record(run, item.entry.key, number, url))
        warning = emit_filed(run.ctx, item.entry.key, str(number), url)
        if warning is not None:
            run.audit_warnings.append(warning)


def _fail_group(
    run: _Run, group: RollupGroup, kind: FilingFailureKind, detail: str
) -> None:
    run.fail_all([item.entry for item in group.items], kind, detail)


def _append_group(run: _Run, group: RollupGroup) -> None:
    """Append *group*'s items to its existing roll-up, re-reading the body first.

    The re-read keeps a checkbox a person ticked since the listing.
    """
    number = group.number
    if number is None:
        return
    timeout = run.timeout(FETCH_BODY_TIMEOUT_S)
    if timeout is None:
        _fail_group(run, group, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return
    body = run.tracker.fetch_issue_body(str(number), timeout=timeout)
    if body is None:
        _fail_group(run, group, FilingFailureKind.NETWORK, _FETCH_BODY_DETAIL)
        return
    timeout = run.timeout(UPDATE_BODY_TIMEOUT_S)
    if timeout is None:
        _fail_group(run, group, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return
    if not run.tracker.update_issue_body(
        number, append_items(body, group.items), timeout=timeout
    ):
        _fail_group(run, group, FilingFailureKind.OTHER, _UPDATE_BODY_DETAIL)
        return
    _record_listed(run, group, number, _known_url(run, number))


def _create_group(run: _Run, group: RollupGroup) -> None:
    """Create a new roll-up part holding *group*'s items."""
    timeout = run.timeout(CREATE_ISSUE_TIMEOUT_S)
    if timeout is None:
        _fail_group(run, group, FilingFailureKind.BUDGET_EXHAUSTED, _BUDGET_DETAIL)
        return
    created = run.tracker.create_issue(
        group_title(group, run.ctx),
        render_body(group.items, run.ctx, group.part),
        labels=[REVIEW_DEBT_LABEL],
        timeout=timeout,
    )
    if created.number is None or created.url is None:
        kind = _GH_KIND[created.failure or GhFailure.OTHER]
        _fail_group(run, group, kind, created.detail or "gh issue create failed")
        return
    _record_listed(run, group, created.number, created.url)


def _file_rollup(
    run: _Run, fresh: list[DebtEntry], lookup: LabelledIssuesLookup
) -> None:
    """List *fresh* in the queue ticket's roll-up, in as many parts as needed."""
    items: list[RollupItem] = [render_item(entry, run.ctx) for entry in fresh]
    for group in plan_groups(items, run.ctx, _target(run, lookup)):
        if group.number is None:
            _create_group(run, group)
        else:
            _append_group(run, group)


def _file_pending(
    run: _Run, pending: list[DebtEntry], lookup: LabelledIssuesLookup
) -> None:
    """Dedup *pending* by marker; list what is left in the roll-up."""
    fresh = [entry for entry in pending if not _marker_hit(run, entry, lookup)]
    if fresh and lookup.truncated:
        run.fail_all(fresh, FilingFailureKind.LOOKUP_FAILED, _TRUNCATED_DETAIL)
        return
    if fresh:
        _file_rollup(run, fresh, lookup)


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
    _file_pending(run, pending, lookup)
    return run.result()
