"""Red-shipped-PR sweep: one page per episode (GitHub #2945, RFC 0016 B2).

A COMPLETED, ``disposition == "shipped"`` row whose OPEN PR hydrates
``ci_ok`` False, has a failing REQUIRED check and is auto-merge-armed is a
shipped PR that will never merge on its own. Nothing reached the operator
before this sweep. Detection is signal-only (ADR-0006, ADR-0014): the row stays
COMPLETED, nothing is parked and no lane slot is held; the only mutation is the
pacing/latch bookkeeping on the row (``TicketTask.shipped_red``).

Per tick (after PR-state hydration), lock-free reads then one locked write:

1. Select candidates (:func:`~cw.dev_queue.shipped_red.is_shipped_red_candidate`),
   due ones first, at most :data:`MAX_EVALUATIONS_PER_TICK`; separately collect
   every latched row that is no longer a candidate (PR went green) to clear.
2. Read ``gh`` (required checks, PR head) outside any lock. ``None`` from the
   required read is *unknown*; ``[]`` is *none failing*. Never test truthiness.
3. Re-validate and stamp the latches under one ``dev_queue_lock``.
4. After the lock is released, emit one ``session.needs_attention``
   (``paused_status: shipped_pr_checks_failed``) per fresh episode.

The page is a pointer, not the evidence: ``cw dev-queue recover-red-pr``
re-reads the live failing checks pinned to the head SHA.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING

from cw import gh
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.dev_queue.shipped_red import (
    MAX_EVALUATIONS_PER_TICK,
    RECHECK_SECONDS,
    SHIPPED_PR_CHECKS_FAILED_REASON,
    episode_marker,
    eval_order_key,
    is_alerted,
    is_due,
    is_shipped_red_candidate,
    red_breadcrumbs,
    red_fingerprint,
    unreadable_backoff_seconds,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus, ShippedRedEpisode

if TYPE_CHECKING:
    from cw.models import DevQueueStore, OrchestratorConfig, TicketTask

_log = logging.getLogger(__name__)

_RowKey = tuple[str, str, str]  # (client, ticket_id, pr_url)


class _Kind(Enum):
    UNKNOWN = auto()  # a gh read failed or could not be parsed: change nothing
    STAMP = auto()  # none failing (optional-only / R5 end) or unarmed: pace
    ALERT = auto()  # a fresh episode: stamp alerted and emit one page
    CONTINUING = auto()  # alerted, same head, same hydrated set: advance only
    REFRESH = auto()  # alerted, same head, new required set: refresh silently


@dataclass(frozen=True)
class _Outcome:
    kind: _Kind
    key: _RowKey
    hydrated_fingerprint: str
    names: tuple[str, ...] = ()
    head_sha: str | None = None


@dataclass(frozen=True)
class _Page:
    ticket_id: str
    client: str
    lane: str
    session_id: str
    pr_url: str
    names: tuple[str, ...]
    head_sha: str
    alerted_at: datetime


def _key(task: TicketTask) -> _RowKey:
    return (task.client, task.ticket_id, task.pr_url or "")


def _hydrated_names(task: TicketTask) -> list[str]:
    return list(task.pr_state.failing_checks) if task.pr_state is not None else []


def _classify_required(
    required: list[gh.RequiredCheck] | None,
    head: gh.PrHead,
    key: _RowKey,
    hydrated: str,
    *,
    same_head: bool,
) -> _Outcome:
    """An alerted row's required read as an outcome (``is None`` before empty)."""
    if required is None:
        return _Outcome(_Kind.UNKNOWN, key, hydrated)
    if len(required) == 0:
        return _Outcome(_Kind.STAMP, key, hydrated)
    names = tuple(c.name for c in required)
    if same_head:
        return _Outcome(_Kind.REFRESH, key, hydrated, names, head.oid)
    # The head moved: the old episode ended; this is a fresh one only if armed.
    kind = _Kind.ALERT if head.auto_merge_armed else _Kind.STAMP
    return _Outcome(kind, key, hydrated, names, head.oid)


def _evaluate_alerted(
    latch: ShippedRedEpisode, key: _RowKey, hydrated: str
) -> _Outcome:
    """Evaluate a row with an open episode: head first, then (maybe) required."""
    head = gh.fetch_pr_head(key[2])
    if head is None:
        return _Outcome(_Kind.UNKNOWN, key, hydrated)
    same_head = head.oid == latch.head_sha
    if same_head and hydrated == latch.hydrated_fingerprint:
        return _Outcome(_Kind.CONTINUING, key, hydrated)
    required = gh.fetch_required_failing_checks(key[2])
    return _classify_required(required, head, key, hydrated, same_head=same_head)


def _evaluate_fresh(key: _RowKey, hydrated: str) -> _Outcome:
    """Evaluate a row with no open episode: required first, then the head."""
    required = gh.fetch_required_failing_checks(key[2])
    if required is None:
        return _Outcome(_Kind.UNKNOWN, key, hydrated)
    if len(required) == 0:
        return _Outcome(_Kind.STAMP, key, hydrated)
    head = gh.fetch_pr_head(key[2])
    if head is None:
        return _Outcome(_Kind.UNKNOWN, key, hydrated)
    if not head.auto_merge_armed:
        return _Outcome(_Kind.STAMP, key, hydrated)
    names = tuple(c.name for c in required)
    return _Outcome(_Kind.ALERT, key, hydrated, names, head.oid)


def _evaluate(task: TicketTask) -> _Outcome:
    key = _key(task)
    hydrated = red_fingerprint(_hydrated_names(task))
    latch = task.shipped_red
    if latch is not None and is_alerted(task):
        return _evaluate_alerted(latch, key, hydrated)
    return _evaluate_fresh(key, hydrated)


def _find_row(store: DevQueueStore, key: _RowKey) -> TicketTask | None:
    for task in store.tasks:
        if _key(task) == key and task.status is QueueItemStatus.COMPLETED:
            return task
    return None


def _stamp_unknown(task: TicketTask, outcome: _Outcome, now: datetime) -> None:
    latch = task.shipped_red
    if latch is None:
        latch = ShippedRedEpisode(
            hydrated_fingerprint=outcome.hydrated_fingerprint, evaluated_at=now
        )
        task.shipped_red = latch
    # Unknown never clears and never advances hydrated_fingerprint: the pending
    # refresh is retried when the not-before passes.
    latch.failures += 1
    latch.retry_not_before = now + timedelta(
        seconds=unreadable_backoff_seconds(latch.failures)
    )
    latch.evaluated_at = now


def _apply_alert(task: TicketTask, outcome: _Outcome, now: datetime) -> _Page:
    head = outcome.head_sha or ""
    task.shipped_red = ShippedRedEpisode(
        hydrated_fingerprint=outcome.hydrated_fingerprint,
        fingerprint=red_fingerprint(outcome.names),
        required_checks=list(outcome.names),
        head_sha=head,
        evaluated_at=now,
        alerted_at=now,
    )
    return _Page(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        session_id=task.session_id or "",
        pr_url=outcome.key[2],
        names=outcome.names,
        head_sha=head,
        alerted_at=now,
    )


def _apply_outcome(task: TicketTask, outcome: _Outcome, now: datetime) -> _Page | None:
    """Stamp *task*'s latch by *outcome*; return the page for a fresh episode."""
    latch = task.shipped_red
    if outcome.kind is _Kind.UNKNOWN:
        _stamp_unknown(task, outcome, now)
    elif outcome.kind is _Kind.STAMP:
        task.shipped_red = ShippedRedEpisode(
            hydrated_fingerprint=outcome.hydrated_fingerprint,
            evaluated_at=now,
            retry_not_before=now + timedelta(seconds=RECHECK_SECONDS),
        )
    elif outcome.kind is _Kind.ALERT:
        return _apply_alert(task, outcome, now)
    elif latch is not None:
        latch.evaluated_at = now
        latch.failures = 0
        if outcome.kind is _Kind.REFRESH:
            latch.required_checks = list(outcome.names)
            latch.fingerprint = red_fingerprint(outcome.names)
            latch.hydrated_fingerprint = outcome.hydrated_fingerprint
            latch.retry_not_before = None
    return None


def _write(
    outcomes: list[_Outcome], clear_keys: list[_RowKey], now: datetime
) -> list[_Page]:
    """Re-validate and stamp under one lock; return the pages to emit after it."""
    pages: list[_Page] = []
    with dev_queue_lock():
        store = load_dev_queue()
        dirty = False
        for key in clear_keys:
            row = _find_row(store, key)
            if row is not None and not is_shipped_red_candidate(row):
                row.shipped_red = None
                dirty = True
        for outcome in outcomes:
            row = _find_row(store, outcome.key)
            if row is None or not is_shipped_red_candidate(row):
                continue
            page = _apply_outcome(row, outcome, now)
            dirty = True
            if page is not None:
                pages.append(page)
        if dirty:
            save_dev_queue(store)
    return pages


def _emit(page: _Page) -> None:
    """The nine canonical attention fields plus the additive red-PR fields."""
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": page.session_id,
            "session_name": "",
            "client": page.client,
            "ticket_id": page.ticket_id,
            "claude_session_id": None,
            "paused_status": SHIPPED_PR_CHECKS_FAILED_REASON,
            "breadcrumbs": red_breadcrumbs(page.names),
            "crashed": False,
            "lane": page.lane,
            "pr_url": page.pr_url,
            "failing_checks": list(page.names),
            "head_sha": page.head_sha,
            "renotify_marker": episode_marker(page.head_sha, page.alerted_at),
        },
        correlation_id=page.ticket_id,
    )


def scan_shipped_red_prs(config: OrchestratorConfig) -> None:
    """One sweep tick; see the module docstring. Best-effort, never raises ``gh``.

    *config* is accepted for the dispatch loop's guarded-step shape; the
    ``shipped_red_signal_enabled`` off-switch is applied by the caller.
    """
    del config  # the off-switch lives in the loop's guarded helper
    now = datetime.now(UTC)
    store = load_dev_queue()
    clear_keys = [
        _key(t)
        for t in store.tasks
        if t.shipped_red is not None and not is_shipped_red_candidate(t)
    ]
    selected = sorted(
        (t for t in store.tasks if is_shipped_red_candidate(t) and is_due(t, now)),
        key=eval_order_key,
    )[:MAX_EVALUATIONS_PER_TICK]
    outcomes = [_evaluate(t) for t in selected]
    if not outcomes and not clear_keys:
        return
    for page in _write(outcomes, clear_keys, now):
        _emit(page)
