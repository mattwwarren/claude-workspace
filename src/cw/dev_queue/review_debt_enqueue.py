"""Auto-enqueue a merged PR's review-debt tickets into a debt lane (#3045).

RFC 0017 D-8 (III3). A ``PR_MERGED`` event for a row whose client sets
``review.debt_autoenqueue`` is turned into queued rows: one PENDING row per
qualifying review-debt ticket, in the configured debt lane, at stage PLAN.

Per event the consumer runs four phases, in this order:

1. Lock-free reads of the client config and the queue store (done once per call).
2. One strict add (``add_ticket_if_absent``) per candidate, each under its own
   ``dev_queue_lock``. An insert's ``ticket.enqueued`` and ``review.debt_enqueued``
   are recorded under that same lock, right after the save, so a persisted row
   never lacks its enqueue audit. No gh or git call, and ``sessions_lock`` is
   never taken.
3. Skip audit events (``review.debt_enqueue_skipped``), emitted after the locks
   are released. A skip writes no row.
4. Cursor advance, per event, only after that event's adds are durable and its
   audit events are recorded.

Replay safety: a crash between an insert and the cursor advance replays the
event, and the existing-row check turns each re-add into a ``row_exists`` skip,
so no sibling row is minted. A crash between an insert and its audit emit stays
at-most-once, which is accepted.

Forward-only: the consumer starts at the inbox end on first run, so enabling
the toggle never back-fills historical merges. Older debt is enqueued by hand
with ``cw dev-queue add``.

The module is not re-exported from ``cw.dev_queue``; its only entry point is
``enqueue_review_debt_for_merged_prs``, called from the dispatch loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.config import load_clients
from cw.dev_queue.crud import (
    StrictAddResult,
    _newest_by_created_at,
    add_ticket_if_absent,
    build_queued_task,
)
from cw.dev_queue.storage import load_dev_queue
from cw.events import (
    advance_cursor,
    init_cursor_at_end,
    load_cursor,
    read_events,
    record_event,
)
from cw.exceptions import (
    LaneNotFoundError,
    LockTimeoutError,
    RequeueStageError,
    UnknownClientError,
)
from cw.gh.pr_reads import parse_pr_url
from cw.models import OrchestratorEventType, Stage

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorEvent,
        TicketTask,
    )
    from cw.models.review_debt_record import ReviewDebtRecord

REVIEW_DEBT_ENQUEUE_CONSUMER = "dev_queue_review_debt_enqueue"
"""Cursor name for the PR_MERGED consumer. Forward-only on first run."""

DEBT_AUTOENQUEUE_PRIORITY = 0
"""Priority for auto-enqueued debt rows. FIFO by ``created_at`` within a lane."""

_EMPTY_INBOX_CURSOR = "__cw_review_debt_enqueue_initialized__"
"""Cursor marker for a first run that observed an empty inbox."""

_log = logging.getLogger(__name__)
_PARTIAL_EMIT_EXCEPTIONS = (Exception,)


class DebtEnqueueSkipReason(StrEnum):
    """Why a candidate was not enqueued (``review.debt_enqueue_skipped``)."""

    ONE_LEVEL = "one_level"
    ROW_EXISTS = "row_exists"
    NO_PARENT_ROW = "no_parent_row"
    LANE_UNDECLARED = "lane_undeclared"
    PR_MISMATCH = "pr_mismatch"
    UNKNOWN_CLIENT = "unknown_client"


@dataclass(frozen=True)
class _MergedPr:
    """The fields of a ``PR_MERGED`` payload this consumer reads."""

    repo: str
    pr_number: int
    client: str
    ticket_id: str


@dataclass(frozen=True)
class _Outcome:
    """One candidate's result: an enqueue (``reason`` None), or a skip."""

    parent_ticket_id: str
    client: str
    lane: str | None
    pr_number: int
    debt_ticket_id: str | None
    key: str | None
    reason: DebtEnqueueSkipReason | None

    def payload(self) -> dict[str, object]:
        return {
            "client": self.client,
            "ticket_id": self.parent_ticket_id,
            "debt_ticket_id": self.debt_ticket_id,
            "lane": self.lane,
            "key": self.key,
            "pr_number": self.pr_number,
        }


def _outcome(
    merged: _MergedPr,
    lane: str | None,
    *,
    debt_ticket_id: str | None,
    key: str | None,
    reason: DebtEnqueueSkipReason | None,
) -> _Outcome:
    return _Outcome(
        parent_ticket_id=merged.ticket_id,
        client=merged.client,
        lane=lane,
        pr_number=merged.pr_number,
        debt_ticket_id=debt_ticket_id,
        key=key,
        reason=reason,
    )


def enqueue_review_debt_for_merged_prs() -> list[str]:
    """Consume new ``PR_MERGED`` events; return the debt ticket IDs enqueued.

    A no-op when the inbox has no new merges. Failures that are not handled
    per record (a lock timeout, an ``OSError``, a broken ``clients.yaml``)
    propagate with the failing event's cursor unadvanced, so the next tick
    retries it. Events consumed before the failure stay consumed.
    """
    init_cursor_at_end(REVIEW_DEBT_ENQUEUE_CONSUMER)
    if load_cursor(REVIEW_DEBT_ENQUEUE_CONSUMER) is None:
        # ``init_cursor_at_end`` cannot persist a cursor for an empty inbox.
        # Keep an explicit marker so a merge appended after this first run is
        # consumed instead of being treated as historical on the next call.
        advance_cursor(REVIEW_DEBT_ENQUEUE_CONSUMER, _EMPTY_INBOX_CURSOR)
    cursor = load_cursor(REVIEW_DEBT_ENQUEUE_CONSUMER)
    if cursor == _EMPTY_INBOX_CURSOR:
        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
    else:
        events = read_events(
            consumer=REVIEW_DEBT_ENQUEUE_CONSUMER,
            event_types=[OrchestratorEventType.PR_MERGED],
        )
    if not events:
        return []
    clients = load_clients()
    store = load_dev_queue()
    enqueued: list[str] = []
    for event in events:
        enqueued.extend(_process_event(event, clients, store))
        advance_cursor(REVIEW_DEBT_ENQUEUE_CONSUMER, event.id)
    return enqueued


def _process_event(
    event: OrchestratorEvent,
    clients: dict[str, ClientConfig],
    store: DevQueueStore,
) -> list[str]:
    """Enqueue one merge's debt candidates. Returns the debt IDs enqueued."""
    merged = _parse_merged(event)
    if merged is None:
        return []
    cfg = clients.get(merged.client)
    if cfg is None:
        # Pre-flight Resolution 12: an unknown client is caught per record, never
        # treated as a quiet "toggle off". The cursor still advances.
        _log.warning(
            "review-debt enqueue: PR_MERGED %s names unknown client %r; skipped",
            event.id,
            merged.client,
        )
        _emit_outcomes(
            [
                _outcome(
                    merged,
                    None,
                    debt_ticket_id=None,
                    key=None,
                    reason=DebtEnqueueSkipReason.UNKNOWN_CLIENT,
                )
            ]
        )
        return []
    lane = _toggle_lane(cfg)
    if lane is None:
        return []
    parent = _select_parent(store, merged)
    if parent is None:
        _emit_outcomes(
            [
                _outcome(
                    merged,
                    lane,
                    debt_ticket_id=None,
                    key=None,
                    reason=DebtEnqueueSkipReason.NO_PARENT_ROW,
                )
            ]
        )
        return []
    outcomes: list[_Outcome] = []
    enqueued: list[str] = []
    try:
        for debt_id, record in _qualifying_records(parent, merged):
            skip = _skip_outcome(debt_id, record, merged, parent, lane)
            if skip is not None:
                outcomes.append(skip)
                continue
            outcome = _enqueue_candidate(debt_id, record.key, merged, lane)
            if outcome is None:
                continue
            if outcome.reason is None:
                enqueued.append(debt_id)
            else:
                outcomes.append(outcome)
    except (LockTimeoutError, OSError):
        # Retryable: emit what the earlier candidates already inserted, so their
        # audit events are not lost, then let the cursor stay put for a retry.
        _emit_partial(outcomes)
        raise
    _emit_outcomes(outcomes)
    return enqueued


def _parse_merged(event: OrchestratorEvent) -> _MergedPr | None:
    """Read a ``PR_MERGED`` payload. A malformed one is logged and consumed."""
    payload = event.payload
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    ticket_id = payload.get("ticket_id")
    client = payload.get("client")
    # bool subclasses int, so a JSON true/false would otherwise pass as a PR number.
    if (
        not isinstance(repo, str)
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or not isinstance(ticket_id, str)
        or not isinstance(client, str)
    ):
        _log.warning("review-debt enqueue: malformed PR_MERGED event %s", event.id)
        return None
    return _MergedPr(repo=repo, pr_number=pr_number, client=client, ticket_id=ticket_id)


def _toggle_lane(cfg: ClientConfig) -> str | None:
    """The client's debt lane, or None when the toggle is off."""
    toggle = cfg.review.debt_autoenqueue
    return None if toggle is None else toggle.lane


def _is_merged_pr(pr_url: str, merged: _MergedPr) -> bool:
    """True iff ``pr_url`` parses to the merged PR (repo compared case-blind)."""
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return False
    repo, number = parsed
    return number == merged.pr_number and repo.lower() == merged.repo.lower()


def _select_parent(store: DevQueueStore, merged: _MergedPr) -> TicketTask | None:
    """The row for ``(client, ticket_id)`` whose ``pr_url`` is the merged PR.

    Duplicate rows are common, so the PR match is what picks one. The tie-break
    among several matching rows is the repo's newest-``created_at`` rule.
    """
    matches = [
        row
        for row in store.tasks
        if row.client == merged.client
        and row.ticket_id == merged.ticket_id
        and row.pr_url is not None
        and _is_merged_pr(row.pr_url, merged)
    ]
    if not matches:
        return None
    return _newest_by_created_at(matches)


def _qualifying_records(
    parent: TicketTask, merged: _MergedPr
) -> list[tuple[str, ReviewDebtRecord]]:
    """``(debt_ticket_id, record)`` pairs for records carrying an ID, deduped by ID.

    An ID is any non-blank ``ticket_id``, on any tracker and in any mode. The
    shape of a Linear ID is checked, not that the ticket exists; there is no
    ticket-state check at merge time. Several records can share one ID (a
    roll-up issue lists many findings); the pair kept for an ID is the first
    record linked to the merged PR (or to no PR yet), else the first record, so
    one finding linked to an earlier PR cannot hide a roll-up this PR carried.
    """
    chosen: dict[str, ReviewDebtRecord] = {}
    for record in parent.review_debt:
        debt_id = record.ticket_id
        if debt_id is None or not debt_id.strip():
            continue
        prior = chosen.get(debt_id)
        if prior is None or (
            _links_other_pr(prior, merged) and not _links_other_pr(record, merged)
        ):
            chosen[debt_id] = record
    return list(chosen.items())


def _links_other_pr(record: ReviewDebtRecord, merged: _MergedPr) -> bool:
    """True when *record* is linked to a PR other than the merged one."""
    return record.pr_url is not None and not _is_merged_pr(record.pr_url, merged)


def _skip_outcome(
    debt_id: str,
    record: ReviewDebtRecord,
    merged: _MergedPr,
    parent: TicketTask,
    lane: str,
) -> _Outcome | None:
    """A skip outcome for this candidate, or None when it may be enqueued.

    ``pr_mismatch`` is checked first, then ``one_level``. Both only skip, so the
    order affects the reported reason, never the outcome. The parent's lane is
    read now, at merge time, not when the debt was filed.
    """
    if _links_other_pr(record, merged):
        reason = DebtEnqueueSkipReason.PR_MISMATCH
    elif parent.lane == lane:
        reason = DebtEnqueueSkipReason.ONE_LEVEL
    else:
        return None
    return _outcome(merged, lane, debt_ticket_id=debt_id, key=record.key, reason=reason)


def _enqueue_candidate(
    debt_id: str,
    key: str,
    merged: _MergedPr,
    lane: str,
) -> _Outcome | None:
    """Strict-add one candidate. Returns its outcome, or None for a no-event drop.

    ``LockTimeoutError`` and ``OSError`` propagate, so the caller leaves the
    cursor put. A bad ticket ID is logged and dropped with no event, so a
    poison record cannot hold the cursor forever.
    """
    try:
        task = build_queued_task(
            debt_id,
            merged.client,
            lane=lane,
            stage=Stage.PLAN,
            priority=DEBT_AUTOENQUEUE_PRIORITY,
        )
    except ValidationError as exc:
        _log.warning(
            "review-debt enqueue: debt ticket %r rejected by TicketTask; skipped: %s",
            debt_id,
            exc,
        )
        return None
    inserted = _outcome(merged, lane, debt_ticket_id=debt_id, key=key, reason=None)
    try:
        result = add_ticket_if_absent(
            task, on_inserted=lambda: _emit_enqueued(inserted)
        )
    except (LaneNotFoundError, RequeueStageError) as exc:
        _log.error(
            "review-debt enqueue: %r into lane %r for client %r refused: %s",
            debt_id,
            lane,
            merged.client,
            exc,
        )
        reason: DebtEnqueueSkipReason | None = DebtEnqueueSkipReason.LANE_UNDECLARED
    except UnknownClientError as exc:
        _log.warning(
            "review-debt enqueue: %r for unknown client %r: %s",
            debt_id,
            merged.client,
            exc,
        )
        reason = DebtEnqueueSkipReason.UNKNOWN_CLIENT
    else:
        reason = (
            DebtEnqueueSkipReason.ROW_EXISTS
            if result is StrictAddResult.ROW_EXISTS
            else None
        )
    if reason is None:
        return inserted
    return _outcome(merged, lane, debt_ticket_id=debt_id, key=key, reason=reason)


def _emit_enqueued(outcome: _Outcome) -> None:
    """Record ``review.debt_enqueued``, under the dev-queue lock after the save."""
    record_event(
        OrchestratorEventType.REVIEW_DEBT_ENQUEUED,
        outcome.payload(),
        correlation_id=outcome.parent_ticket_id,
    )


def _emit_partial(outcomes: list[_Outcome]) -> None:
    """Emit on the retryable-error path without masking the original exception."""
    try:
        _emit_outcomes(outcomes)
    except _PARTIAL_EMIT_EXCEPTIONS:
        _log.exception(
            "review-debt enqueue: partial-batch emit failed; original error kept"
        )


def _emit_outcomes(outcomes: list[_Outcome]) -> None:
    """Record skip audit events, after the locks are released.

    Inserted rows are not here: their events were recorded under the lock.
    """
    for outcome in outcomes:
        if outcome.reason is None:
            continue
        record_event(
            OrchestratorEventType.REVIEW_DEBT_ENQUEUE_SKIPPED,
            {**outcome.payload(), "reason": outcome.reason.value},
            correlation_id=outcome.parent_ticket_id,
        )
