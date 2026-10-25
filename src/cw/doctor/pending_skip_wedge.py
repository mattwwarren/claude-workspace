"""Doctor wedge class for a PENDING row held by an occupied worktree (#2516).

``wedge/pending-worktree-occupied``: a PENDING queue row that dispatch has
skipped as ``worktree_occupied`` on at least
:data:`PENDING_SKIP_WEDGE_THRESHOLD` consecutive ticks, the newest of them
fresh. A live session or daemon worker holds the ticket's worktree and never
releases it; the client-wide tick meanwhile reads ``no_pending``, so nothing
else names the stall.

Advisory only. :func:`_check_wedge_pending_worktree_occupied` reads the
inbox and reports; it records no event and mutates nothing. ``cw doctor
--reap`` leaves the row alone: the class is listed in the
``running_ticket_ids`` exclusion set of ``cw.doctor.wedge.reap``, which is
default-inclusive and would otherwise revert a RUNNING row of the same
ticket. A stale streak (newest event older than ``TICK_STALE_SECONDS``) is
not reported: the dispatch loop itself has stopped ticking, which the
loop-liveness check owns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import state_file
from cw.doctor._shared import WedgeFinding
from cw.events import read_events
from cw.lane_pause import iso_z
from cw.models import DispatchSkipReason, OrchestratorEventType, QueueItemStatus
from cw.pending_skip_streak import pending_skip_streaks

if TYPE_CHECKING:
    from cw.models import DevQueueStore
    from cw.pending_skip_streak import SkipStreak

WEDGE_PENDING_WORKTREE_OCCUPIED = "wedge/pending-worktree-occupied"

# About 30 minutes at the default 30s tick: long past any live session's
# normal worktree release, short enough to surface the same working day.
PENDING_SKIP_WEDGE_THRESHOLD = 60


def _recipe(streak: SkipStreak) -> str:
    ticket, client = streak.ticket_id, streak.client
    return (
        f"Ticket {ticket} (client {client}) PENDING but skipped"
        f" {streak.skip_reason} for {streak.consecutive} consecutive dispatch"
        f" ticks (since {iso_z(streak.first_at)}); it will not clear itself if"
        f" the holder is dead. Inspect: cw dev-queue show {ticket} -c {client}."
        " If the occupant is dead: cw spawn close --confirmed-dead <id>, then"
        " cw dev-queue requeue / approve. Also check wedge/leaked-daemon-worker"
        " and cw doctor --reap."
    )


def _is_wedged(streak: SkipStreak) -> bool:
    return (
        streak.skip_reason == DispatchSkipReason.WORKTREE_OCCUPIED.value
        and streak.consecutive >= PENDING_SKIP_WEDGE_THRESHOLD
        and not streak.stale
    )


def _check_wedge_pending_worktree_occupied(
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Report every PENDING row held by a long, fresh worktree_occupied streak.

    Reads the inbox once, and not at all when no row is PENDING. Findings
    are sorted by ``(client, ticket_id)``.
    """
    pending_rows = [t for t in queue.tasks if t.status == QueueItemStatus.PENDING]
    if not pending_rows:
        return []
    events = read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])
    streaks = pending_skip_streaks(pending_rows, events, now=datetime.now(UTC))
    path = str(state_file())
    return [
        WedgeFinding(
            wedge_class=WEDGE_PENDING_WORKTREE_OCCUPIED,
            session_id=None,
            ticket_id=streak.ticket_id,
            recipe=_recipe(streak),
            state_file=path,
        )
        for _key, streak in sorted(streaks.items())
        if _is_wedged(streak)
    ]
