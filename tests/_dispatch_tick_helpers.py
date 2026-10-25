"""Shared ``dispatch.tick`` event builders and seeder (#2516).

One home for the two ``dispatch.tick`` shapes the dispatch loop records: the
client-wide tick (no ``ticket_id``) and the per-ticket skip tick that
``_emit_worktree_occupied_skip_event`` records. Used by
``test_pending_skip_streak.py``, ``test_doctor_pending_skip_wedge.py``,
``test_cli_dev_queue_status.py`` and ``test_doctor.py``. No ``test_`` prefix,
so pytest does not collect it (same convention as
``tests/_ticket_view_helpers.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from freezegun import freeze_time

from cw.events import record_event
from cw.models import OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

_DEFAULT_STEP = timedelta(seconds=30)


def client_tick_event(
    client: str, at: datetime, **payload: object
) -> OrchestratorEvent:
    """An in-memory client-wide ``dispatch.tick`` (no ``ticket_id``) at *at*."""
    body: dict[str, object] = {
        "client": client,
        "claimed": 0,
        "pending": 1,
        "running": 0,
        "cap": 3,
        "skip_reason": "no_pending",
    }
    body.update(payload)
    return OrchestratorEvent(
        type=OrchestratorEventType.DISPATCH_TICK, payload=body, created_at=at
    )


def skip_tick_events(
    client: str,
    ticket_id: str,
    reason: str,
    *,
    count: int,
    start: datetime,
    step: timedelta = _DEFAULT_STEP,
) -> list[OrchestratorEvent]:
    """*count* in-memory per-ticket skip ticks, ascending from *start* by *step*."""
    return [
        OrchestratorEvent(
            type=OrchestratorEventType.DISPATCH_TICK,
            payload={
                "client": client,
                "claimed": 0,
                "skip_reason": reason,
                "ticket_id": ticket_id,
            },
            created_at=start + i * step,
        )
        for i in range(count)
    ]


def seed_dispatch_events(events: Iterable[OrchestratorEvent]) -> None:
    """Record each event in the tmp inbox, stamped at its own ``created_at``."""
    for event in events:
        with freeze_time(event.created_at):
            record_event(event.type, event.payload)
