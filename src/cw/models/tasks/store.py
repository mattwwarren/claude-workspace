"""``occupies_lane_slot``, ``DispatchPlan`` and ``DevQueueStore``.

``DevQueueStore`` is the persisted ``dev_queue.json`` root. Imports
``ticket_task`` and ``schema``. ``WatchedPr`` is imported (not merely
referenced) because ``DevQueueStore.watched_prs`` resolves at class-build time
under ``from __future__ import annotations``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cw._predicates import is_positive_real_int
from cw.models.enums import (
    DEPENDENCY_HOLD_DISPOSITION,
    OCCUPIED_LANE_STATUSES,
    TERMINAL_SESSION_STATUSES,
    QueueItemStatus,
    ReapReason,
)
from cw.models.events import WatchedPr
from cw.models.tasks.schema import DEV_QUEUE_SCHEMA_VERSION
from cw.models.tasks.ticket_task import TicketTask

if TYPE_CHECKING:
    from cw.models.enums import SessionStatus


def _parked_worker_settled(
    task: TicketTask, session_statuses: Mapping[str, SessionStatus] | None
) -> bool:
    """True iff a BLOCKED_ON_USER *task* has no live worker and no unsettled claim.

    GitHub #3774 (R4). ``None`` is no session evidence, so the park counts.
    An absent session id is unsettled (fail closed): a lost record cannot
    prove the worker is gone. ``hook_context_conflict_session_id`` is not
    consulted: it names another session's context, not this row's worker (D4).
    """
    if session_statuses is None:
        return False
    if task.fix_dispatch_launched_worker is not None or task.launch_marker is not None:
        return False
    for sid in (
        task.session_id,
        task.codex_orphan_session_id,
        task.fix_dispatch_session_id,
    ):
        if sid is None:
            continue
        status = session_statuses.get(sid)
        if status is None or status not in TERMINAL_SESSION_STATUSES:
            return False
    return True


def occupies_lane_slot(
    task: TicketTask,
    *,
    session_statuses: Mapping[str, SessionStatus] | None = None,
) -> bool:
    """True iff *task* counts toward its lane's ``OCCUPIED_LANE_STATUSES`` cap.

    Three carve-outs free a BLOCKED_ON_USER slot, and nowhere else:

    - ``terminal_sibling`` (GitHub #2100): a duplicate row whose ticket already
      reached a terminal status.
    - id-bearing ``dependency_hold`` (GitHub #2321): waits on a merge the serve
      tick observes.
    - settled-worker park (GitHub #3774, R4): the row has no live worker and no
      unsettled claim. Settled means every named session is terminal, and
      ``fix_dispatch_launched_worker`` and ``launch_marker`` are unset.

    ``session_statuses`` maps session id to status for the consumer's scope.
    Every production consumer must pass the tick's map. ``None`` means no
    evidence: the park counts (fail closed, the legacy behaviour).
    """
    if task.status not in OCCUPIED_LANE_STATUSES:
        return False
    if task.status == QueueItemStatus.BLOCKED_ON_USER:
        if task.disposition == ReapReason.TERMINAL_SIBLING.value:
            return False
        # GitHub #2321 — a dependency hold with a PR id frees its slot.
        if task.disposition == DEPENDENCY_HOLD_DISPOSITION and is_positive_real_int(
            task.blocked_on_pr
        ):
            return False
        return not _parked_worker_settled(task, session_statuses)
    return True


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
