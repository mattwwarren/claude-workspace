"""Per-client live queue-row counts shared by ``status`` and ``status --json`` (#1586).

The text table's six count columns and the matching ``--json`` keys come from
one place, so the two surfaces cannot drift apart. ``blocked`` counts
``BLOCKED_ON_USER`` rows only: ``AWAITING_OPERATOR_SIGNOFF`` rows appear in none
of the six counts, so ``pending + running + blocked`` is not lane occupancy.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.models import QueueItemStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from cw.models import TicketTask


@dataclass(frozen=True)
class StatusCounts:
    """Row counts for one client; field names are the ``--json`` keys."""

    pending: int = 0
    running: int = 0
    blocked: int = 0
    completed: int = 0
    cancelled: int = 0
    failed: int = 0

    @classmethod
    def from_tasks(cls, tasks: Iterable[TicketTask]) -> StatusCounts:
        """Count *tasks* by status; uncounted statuses are ignored."""
        tally = Counter(t.status for t in tasks)
        return cls(
            pending=tally[QueueItemStatus.PENDING],
            running=tally[QueueItemStatus.RUNNING],
            blocked=tally[QueueItemStatus.BLOCKED_ON_USER],
            completed=tally[QueueItemStatus.COMPLETED],
            cancelled=tally[QueueItemStatus.CANCELLED],
            failed=tally[QueueItemStatus.FAILED],
        )


def status_counts_by_client(tasks: Sequence[TicketTask]) -> dict[str, StatusCounts]:
    """:class:`StatusCounts` for each client that has at least one row in *tasks*."""
    grouped: dict[str, list[TicketTask]] = {}
    for task in tasks:
        grouped.setdefault(task.client, []).append(task)
    return {name: StatusCounts.from_tasks(rows) for name, rows in grouped.items()}
