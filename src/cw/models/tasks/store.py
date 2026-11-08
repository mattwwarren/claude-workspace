"""``occupies_lane_slot``, ``DispatchPlan`` and ``DevQueueStore``.

``DevQueueStore`` is the persisted ``dev_queue.json`` root. Imports
``ticket_task`` and ``schema``. ``WatchedPr`` is imported (not merely
referenced) because ``DevQueueStore.watched_prs`` resolves at class-build time
under ``from __future__ import annotations``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cw.models.enums import OCCUPIED_LANE_STATUSES, QueueItemStatus, ReapReason
from cw.models.events import WatchedPr
from cw.models.tasks.schema import DEV_QUEUE_SCHEMA_VERSION
from cw.models.tasks.ticket_task import TicketTask


def occupies_lane_slot(task: TicketTask) -> bool:
    """True iff *task* counts toward its lane's ``OCCUPIED_LANE_STATUSES`` cap.

    GitHub #2100. Mirrors ``OCCUPIED_LANE_STATUSES`` membership with exactly
    one carve-out: a BLOCKED_ON_USER row parked with
    ``disposition == ReapReason.TERMINAL_SIBLING`` (stamped by
    ``cw.reconcile.tasks.park_terminal_sibling_tasks``) is a duplicate row a
    lock-contention race minted for a ticket whose real row already reached a
    terminal status — not live or operator-parked work. Counting it toward
    lane occupancy holds that lane slot forever behind a ticket that already
    shipped, and ``cw doctor --reap`` cannot free it (there is nothing to
    revert it *to* — see the dedicated wedge class in ``cw.doctor.wedge``).

    Every dispatch consumer that gates on ``OCCUPIED_LANE_STATUSES`` for lane
    capacity (``dispatch.tick``/``dispatch.claim``/``board``) must call this
    instead of testing membership directly, so a terminal_sibling row is
    excluded everywhere occupancy is computed, not just where it happens to
    be visible first.
    """
    if task.status not in OCCUPIED_LANE_STATUSES:
        return False
    return not (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == ReapReason.TERMINAL_SIBLING.value
    )


class DispatchPlan(BaseModel):
    """Ordered list of tickets to dispatch, with optional grouping hints."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    tasks: list[TicketTask] = Field(default_factory=list)
    grouping_hints: dict[str, str] = Field(default_factory=dict)


class DevQueueStore(BaseModel):
    """Persisted dev-queue state holding TicketTasks."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    schema_version: int = DEV_QUEUE_SCHEMA_VERSION
    tasks: list[TicketTask] = Field(default_factory=list)
    watched_prs: list[WatchedPr] = Field(default_factory=list)

    def pending(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.PENDING]

    def running(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.RUNNING]

    def completed(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.COMPLETED]

    def cancelled(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.CANCELLED]

    def by_client(self, client: str) -> list[TicketTask]:
        return [t for t in self.tasks if t.client == client]
