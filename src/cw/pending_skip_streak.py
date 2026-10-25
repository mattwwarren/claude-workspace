"""Per-ticket consecutive dispatch-skip streaks, derived from the inbox (#2516).

The dispatch loop records a per-ticket ``dispatch.tick`` (payload ``client``,
``ticket_id``, ``skip_reason``) each time it passes over a PENDING row for a
ticket-specific reason such as ``worktree_occupied``. Nothing persists a count
of those skips on the row, so a ticket held every tick by a worktree that is
never released looks, from the client-wide tick, like ``no_pending``.

:func:`pending_skip_streaks` rebuilds the streak at read time. It is pure (no
I/O): ``cw dev-queue status`` and ``cw doctor``
(``wedge/pending-worktree-occupied``) each read the inbox once and pass the
events in.

A streak is the leading run of a ticket's per-ticket ticks, newest first, that
share the newest event's reason. The run ends at a different per-ticket
reason, at a gap between adjacent events longer than
:data:`STREAK_MAX_GAP_SECONDS`, or at an event older than the row's
``created_at``. ``claimed_at`` is deliberately not a floor: the post-claim
release path re-stamps it every tick. Client-wide ticks (no ``ticket_id``) do
not take part, so they neither count nor break the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.dispatch import TICK_STALE_SECONDS
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from cw.models import OrchestratorEvent, TicketTask

# Longest gap between two adjacent skip events that still counts as one run:
# twenty default (30s) ticks, wide enough to ride out a loop restart, narrow
# enough that a ticket skipped yesterday and again today is two runs.
STREAK_MAX_GAP_SECONDS = 600


@dataclass(frozen=True)
class SkipStreak:
    """One PENDING ticket's run of identical per-ticket skip events."""

    client: str
    ticket_id: str
    skip_reason: str
    consecutive: int
    first_at: datetime
    last_at: datetime
    stale: bool


@dataclass(frozen=True)
class _SkipEvent:
    at: datetime
    reason: str


def _per_ticket_skip(
    event: OrchestratorEvent,
) -> tuple[tuple[str, str], _SkipEvent] | None:
    """``((client, ticket_id), skip)`` for a well-formed per-ticket tick, else None."""
    if event.type is not OrchestratorEventType.DISPATCH_TICK:
        return None
    client = event.payload.get("client")
    ticket_id = event.payload.get("ticket_id")
    reason = event.payload.get("skip_reason")
    if not (
        isinstance(client, str)
        and isinstance(ticket_id, str)
        and isinstance(reason, str)
    ):
        return None
    return (client, ticket_id), _SkipEvent(at=event.created_at, reason=str(reason))


def _events_by_key(
    events: Iterable[OrchestratorEvent],
) -> dict[tuple[str, str], list[_SkipEvent]]:
    """Per-ticket skip events grouped by ``(client, ticket_id)``, newest first."""
    grouped: dict[tuple[str, str], list[_SkipEvent]] = {}
    for event in events:
        parsed = _per_ticket_skip(event)
        if parsed is not None:
            key, skip = parsed
            grouped.setdefault(key, []).append(skip)
    for skips in grouped.values():
        skips.sort(key=lambda s: s.at, reverse=True)
    return grouped


def _leading_run(events_desc: list[_SkipEvent], floor: datetime) -> list[_SkipEvent]:
    """The newest-first run sharing the newest reason, cut at a gap or *floor*."""
    run: list[_SkipEvent] = []
    for skip in events_desc:
        if skip.at < floor:
            break
        if run:
            gap = (run[-1].at - skip.at).total_seconds()
            if skip.reason != run[0].reason or gap > STREAK_MAX_GAP_SECONDS:
                break
        run.append(skip)
    return run


def pending_skip_streaks(
    rows: Iterable[TicketTask],
    events: Iterable[OrchestratorEvent],
    *,
    now: datetime,
) -> dict[tuple[str, str], SkipStreak]:
    """The current skip streak of every PENDING row that has one.

    Keyed by ``(client, ticket_id)``. A PENDING row with no per-ticket event
    newer than its ``created_at`` is absent. ``stale`` is true when the newest
    event is more than ``TICK_STALE_SECONDS`` older than *now*.
    """
    floors: dict[tuple[str, str], datetime] = {}
    for row in rows:
        if row.status != QueueItemStatus.PENDING:
            continue
        key = (row.client, row.ticket_id)
        floors[key] = max(row.created_at, floors.get(key, row.created_at))
    if not floors:
        return {}
    result: dict[tuple[str, str], SkipStreak] = {}
    for key, skips in _events_by_key(events).items():
        floor = floors.get(key)
        if floor is None:
            continue
        run = _leading_run(skips, floor)
        if not run:
            continue
        last_at = run[0].at
        result[key] = SkipStreak(
            client=key[0],
            ticket_id=key[1],
            skip_reason=run[0].reason,
            consecutive=len(run),
            first_at=run[-1].at,
            last_at=last_at,
            stale=(now - last_at).total_seconds() > TICK_STALE_SECONDS,
        )
    return result
