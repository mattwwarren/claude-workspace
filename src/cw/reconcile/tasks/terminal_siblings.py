"""Terminal-sibling parking for reconcile (#2631 package split).

A BLOCKED_ON_USER or PENDING task whose ticket already has a terminal sibling
row is parked so it stops holding a lane slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import load_clients, load_orchestrator_config
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
)
from cw.reconcile.tasks._client_config import (
    _resolve_task_policy,
)

if TYPE_CHECKING:
    from datetime import datetime

_TERMINAL_SIBLING_STATUSES = frozenset(
    [QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED]
)


def _latest_terminal_at_index(
    store: DevQueueStore,
) -> dict[tuple[str, str], datetime]:
    """Return (client, ticket_id) → max(created_at) for all terminal rows."""
    index: dict[tuple[str, str], datetime] = {}
    for t in store.tasks:
        if t.status in _TERMINAL_SIBLING_STATUSES:
            key = (t.client, t.ticket_id)
            if key not in index or t.created_at > index[key]:
                index[key] = t.created_at
    return index


def park_terminal_sibling_tasks() -> list[str]:
    """Park PENDING tasks whose (client, ticket_id) has a terminal sibling.

    A "terminal sibling" is a COMPLETED or CANCELLED task for the same
    (client, ticket_id) across all lanes. Catches stale PENDING rows that
    keep churning after a ticket already reached a terminal state (GitHub #876).

    Per ADR-0006 signal-only posture:
    - ReapPolicy.SIGNAL_ONLY (default): PENDING → BLOCKED_ON_USER + emit
      SESSION_REAP_PROPOSED(reason="terminal_sibling").
    - ReapPolicy.AUTO: PENDING → CANCELLED.

    Does NOT require sessions_lock — operates on dev_queue only. Returns the
    list of ticket_ids that were parked or cancelled.

    Ordering guard: a PENDING task whose created_at is older than any of its
    terminal siblings is skipped. This prevents false-firing on doctor's
    _collapse_blocked_on_user_tasks, which creates CANCELLED rows newer than
    the PENDING it just reverted (oldest PENDING, newer CANCELLED pattern).
    A genuine stale PENDING from the enqueue-dedup gap is always NEWER than
    the original COMPLETED/CANCELLED row.

    # Why: events are collected under dev_queue_lock and emitted after it
    # releases (ADR-0019 permits either).
    """
    # Cheap pre-read outside the lock to fast-exit when no terminal rows exist.
    pre_store = load_dev_queue()
    if not any(t.status in _TERMINAL_SIBLING_STATUSES for t in pre_store.tasks):
        return []

    # Load policy config outside the lock (no lock-order constraint).
    orchestrator_config = load_orchestrator_config()
    clients = load_clients()

    parked_ids: list[str] = []
    # Snapshot fields needed for event emission after the lock.
    pending_events: list[tuple[str, str, str, str | None]] = []

    with dev_queue_lock():
        store = load_dev_queue()
        latest_terminal_at = _latest_terminal_at_index(store)
        if not latest_terminal_at:
            return []

        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.PENDING:
                continue
            key = (task.client, task.ticket_id)
            if key not in latest_terminal_at:
                continue
            # Ordering guard: skip PENDING tasks older than their terminal siblings.
            # Genuine stale PENDING (enqueue-dedup gap) is inserted AFTER the
            # terminal row — task.created_at >= terminal.created_at.
            # Doctor's collapse reverts the OLDEST task to PENDING and creates
            # newer CANCELLED rows — task.created_at < latest_terminal.created_at.
            if task.created_at < latest_terminal_at[key]:
                continue
            policy = _resolve_task_policy(
                task.client, task.lane, clients, orchestrator_config
            )
            orig_session_id = task.session_id
            if policy is ReapPolicy.AUTO:
                # Why: CANCELLED is in _RESET_DISPOSITION_STATUSES — disposition is
                # cleared regardless; reason is captured in SESSION_REAP_PROPOSED event.
                transition_task_status(task, QueueItemStatus.CANCELLED)
            else:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=ReapReason.TERMINAL_SIBLING.value,
                )
            task.session_id = None
            parked_ids.append(task.ticket_id)
            pending_events.append(
                (task.ticket_id, task.client, task.lane, orig_session_id)
            )
            changed = True
        if changed:
            save_dev_queue(store)

    # Emit events after dev_queue_lock releases (ADR-0019 permits either).
    for ticket_id, client, lane, session_id in pending_events:
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            {
                "session_id": session_id,
                "session_name": None,
                "client": client,
                "ticket_id": ticket_id,
                "lane": lane,
                "proposed_action": "terminal_sibling",
                "reason": ReapReason.TERMINAL_SIBLING.value,
                "evidence": {},
            },
            correlation_id=ticket_id,
        )

    return parked_ids
