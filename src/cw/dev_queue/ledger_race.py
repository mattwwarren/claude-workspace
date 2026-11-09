"""Shared refuse/re-check protocol for a convergence-ledger write (#2834).

A ledger write runs against a dev-queue row that a worker may start on at any
time. The protocol every ledger writer uses is:

1. Refuse while the row is ``RUNNING``: a live worker owns the worktree's ledger.
2. Load the ledger and write it.
3. Re-look the row up after the write. If it became ``RUNNING`` in between, one
   of the two writes may have been lost, so the caller warns and fails; re-running
   the same command is idempotent.

``cw review converge operator-settle`` (``cw.cli.review.operator_settle``) and
the MUST_FIX override (``cw.dev_queue.must_fix_override``) both use these
helpers. There is deliberately no ledger-level lock: a lock that only some
writers take does not serialize the others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError
from cw.models import QueueItemStatus

if TYPE_CHECKING:
    from cw.models import TicketTask

ROW_STARTED_RUNNING_WARNING = "row_started_running"


def require_row_not_running(row: TicketTask, message: str) -> None:
    """Raise ``CwError(message)`` while *row* is ``RUNNING``."""
    if row.status is QueueItemStatus.RUNNING:
        raise CwError(message)


def row_started_running(ticket_id: str, client_name: str) -> bool:
    """Whether the row was started by a worker since it was last looked up.

    Reloads the dev queue from disk, so it sees a worker that started after the
    caller's own load.
    """
    row = _find_ticket(load_dev_queue(), ticket_id, client_name)
    return row.status is QueueItemStatus.RUNNING
