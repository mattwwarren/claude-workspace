"""Review-debt records on the cw dev-queue row (GitHub #3041, RFC 0017 D-7).

``cw review adjudicate`` and ``cw review debt`` file or emit one debt ticket
per ticketless deferral (``cw.review_debt_filing``), then record what they did
here, onto :attr:`TicketTask.review_debt` of the row ``_find_ticket`` selects
(live, then parked, then newest terminal). The list is the authoritative
dedup store the next filing run reads first.

Modelled on :mod:`cw.dev_queue.review_records`: take the dev-queue lock, load,
find the row, mutate, save. Every ``gh`` call happens before this, outside any
lock (ADR-0019): the caller files first and records after, so this module
never spawns a process and never takes ``sessions_lock``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_debt_record import ReviewDebtRecord

_log = logging.getLogger(__name__)


def _upsert(
    existing: Sequence[ReviewDebtRecord], incoming: Sequence[ReviewDebtRecord]
) -> list[ReviewDebtRecord]:
    """Merge *incoming* into *existing* by ``key``, order kept.

    A replaced record keeps its stored ``pr_url`` when the incoming one has
    none and names the same ticket, so a marker recovery or a verification
    never unlinks a PR. A record for a different ticket (a newly filed one
    replacing a closed ticket, #3044) starts unlinked: the old PR belonged to
    the old ticket.
    """
    merged = list(existing)
    index = {record.key: position for position, record in enumerate(merged)}
    for record in incoming:
        position = index.get(record.key)
        if position is None:
            index[record.key] = len(merged)
            merged.append(record)
            continue
        prior = merged[position]
        keep_pr = (
            record.pr_url is None
            and prior.pr_url is not None
            and record.ticket_id == prior.ticket_id
        )
        merged[position] = (
            record.model_copy(update={"pr_url": prior.pr_url}) if keep_pr else record
        )
    return merged


def read_review_debt(ticket_id: str, client_name: str) -> list[ReviewDebtRecord]:
    """The review-debt records on *ticket_id*'s row, read lock-free.

    Raises:
        CwError: if no row exists for ``(ticket_id, client_name)``.
    """
    task = _find_ticket(load_dev_queue(), ticket_id, client_name, warn=False)
    return list(task.review_debt)


def record_review_debt(
    ticket_id: str,
    client_name: str,
    records: Sequence[ReviewDebtRecord],
) -> list[ReviewDebtRecord]:
    """Upsert *records* by ``key`` onto *ticket_id*'s row; return the merged list.

    Under the dev-queue lock only.

    Raises:
        CwError: if no row exists for ``(ticket_id, client_name)``.
        OSError: if the store could not be written.
    """
    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        merged = _upsert(task.review_debt, records)
        task.review_debt = merged
        save_dev_queue(store)
    _log.info(
        "Recorded %d review-debt record(s) on %s/%s",
        len(records),
        client_name,
        ticket_id,
    )
    return merged
