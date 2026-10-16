"""Operator review decisions recorded on the cw dev-queue row (#2701).

``cw review settle`` writes its ledger here, onto
:attr:`TicketTask.finding_dispositions` of the row ``_find_ticket`` selects
(live, then parked, then newest terminal). That field is the ledger every
review pass reads first, whatever the ticket's tracker is, and the same one
the review pass itself folds a GitHub marker comment into, so a settle
recorded here applies on a Linear-tracked ticket exactly as on a GitHub one.

Modelled on :mod:`cw.dev_queue.must_fix_override`: take the dev-queue lock,
load, find the row, mutate, save. The merge goes through
:func:`~cw.review_finding_dispositions.merge_finding_dispositions`, the one
write chokepoint every ledger write already shares, so it is additive,
provenance-gated and newest-``recorded_at``-wins. No new merge code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.models import QueueItemStatus
from cw.review_finding_dispositions import merge_finding_dispositions

if TYPE_CHECKING:
    from cw.models import TicketTask
    from cw.review_finding_dispositions import FindingDisposition

_log = logging.getLogger(__name__)

#: Row statuses whose ledger is NOT carried onto a re-enqueued row: ``cw
#: dev-queue add`` after one of these creates a fresh row, so a settle that
#: lands here is lost on re-enqueue (#2701 R9). Deliberately NOT
#: ``lifecycle._TERMINAL_STATUSES``, which also holds the parked statuses
#: (BLOCKED_ON_USER, AWAITING_OPERATOR_SIGNOFF): a parked row is released by
#: requeue or approve, which keep the row and its ledger.
TERMINAL_ROW_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
    }
)


class RecordedDispositions(NamedTuple):
    """What :func:`record_finding_dispositions` did to the row."""

    #: The status of the row the ledger landed on.
    row_status: QueueItemStatus
    #: Keys, sorted, whose stored entry is not the one passed in: the row
    #: already held a newer record for the key, or the entry failed provenance.
    not_applied: tuple[str, ...]


def find_ticket_row(ticket_id: str, client_name: str) -> TicketTask:
    """The row a settle for *ticket_id* would land on, read lock-free.

    A pre-flight read: the answer can be stale by the time a write takes the
    lock, which :func:`record_finding_dispositions` re-resolves under it.

    Raises:
        CwError: if no row exists for ``(ticket_id, client_name)``; the
            message names ``cw dev-queue add``.
    """
    return _find_ticket(load_dev_queue(), ticket_id, client_name)


def record_finding_dispositions(
    ticket_id: str,
    client_name: str,
    dispositions: dict[str, FindingDisposition],
    *,
    bounded: bool = False,
) -> RecordedDispositions:
    """Merge *dispositions* onto *ticket_id*'s dev-queue row ledger.

    Under the dev-queue lock (bounded when *bounded*, so an operator entry
    point fails fast rather than waiting on a wedged holder).

    Raises:
        CwError: if no row exists for ``(ticket_id, client_name)``.
        LockTimeoutError: if *bounded* and the lock could not be taken.
        OSError: if the store could not be written.
    """
    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        merged = merge_finding_dispositions(task.finding_dispositions, dispositions)
        task.finding_dispositions = merged
        save_dev_queue(store)
    not_applied = tuple(
        sorted(key for key, entry in dispositions.items() if merged.get(key) != entry)
    )
    _log.info(
        "Recorded %d finding disposition(s) on %s/%s (%s row); %d not applied",
        len(dispositions) - len(not_applied),
        client_name,
        ticket_id,
        task.status.value,
        len(not_applied),
    )
    return RecordedDispositions(task.status, not_applied)
