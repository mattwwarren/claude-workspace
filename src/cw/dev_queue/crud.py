"""Dev-queue CRUD: enqueue, remove, cancel, move, clear, resolve, find.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
operator-facing queue mutations (``add_ticket``, ``register_watched_pr``,
``remove_ticket``, ``cancel_ticket``, ``cancel_task_for_session``,
``move_ticket``, ``clear_tickets``), the read/resolution helpers
(``resolve_client``, ``list_tickets``, ``_newest_by_created_at``,
``_find_ticket``), and the ``task.deleted`` event chokepoint
(``_emit_task_deleted``).

Layering: imports ``lifecycle.transition_task_status`` at module level for the
cancel paths. ``lifecycle.wait_for_terminal`` reaches back into ``_find_ticket``
here via a deferred import to keep this edge one-way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.config import get_client
from cw.dev_queue.lifecycle import transition_task_status
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError, LaneMoveError, LaneNotFoundError
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from cw.models import (
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
        WatchedPr,
    )

_UNMOVABLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)

# Statuses eligible for approve_ticket (an existing BLOCKED_ON_USER approval
# gate, or a parked operator-signoff gate to clear). See GitHub #990.
_APPROVABLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.BLOCKED_ON_USER, QueueItemStatus.AWAITING_OPERATOR_SIGNOFF]
)


def add_ticket(task: TicketTask) -> bool:
    """Enqueue a TicketTask, acquiring the file lock atomically.

    Returns True if the task was inserted, False if a task with the same
    (client, ticket_id) is already PENDING or RUNNING, or if the same
    (client, ticket_id, stage) is already COMPLETED or CANCELLED
    (deduplication guard — terminal check is stage-scoped to allow normal
    multi-stage progression, e.g. COMPLETED PLAN does not block IMPL, #876).

    Raises:
        LaneNotFoundError: if task.lane is not declared for the client.
    """
    _active = {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    _terminal = {QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED}
    with _lock():
        try:
            client_cfg = get_client(task.client)
        except CwError:
            pass  # Unknown client — lane validation deferred to dispatch
        else:
            declared_lane_names = [ln.name for ln in client_cfg.effective_lanes]
            if task.lane not in declared_lane_names:
                msg = (
                    f"Lane '{task.lane}' is not declared for client '{task.client}'."
                    f" Declared lanes: {', '.join(declared_lane_names)}."
                    f" Run: cw lane add {task.client} {task.lane}"
                )
                raise LaneNotFoundError(msg)
        store = load_dev_queue()
        for existing in store.tasks:
            if existing.client != task.client or existing.ticket_id != task.ticket_id:
                continue
            if existing.status in _active:
                return False
            if existing.status in _terminal and existing.stage == task.stage:
                return False
        store.tasks.append(task)
        save_dev_queue(store)
    return True


def register_watched_pr(watched: WatchedPr) -> bool:
    """Insert a WatchedPr into the store's watched_prs list, atomically.

    Returns True if inserted, False if an ``active`` watched PR with the same
    ``(repo, pr_number)`` already exists (idempotency dedup, RFC 0011 S2 R7 —
    mirrors ``add_ticket``'s ``with _lock(): load -> scan -> append/return
    False -> save`` shape). The guard is scoped to ``status == "active"`` so a
    future ``dismissed`` transition can re-open registration (adopted #5).
    """
    with _lock():
        store = load_dev_queue()
        for existing in store.watched_prs:
            if (
                existing.repo == watched.repo
                and existing.pr_number == watched.pr_number
                and existing.status == "active"
            ):
                return False
        store.watched_prs.append(watched)
        save_dev_queue(store)
    return True


def _emit_task_deleted(
    removed: TicketTask, reason: Literal["operator_remove", "operator_clear"]
) -> None:
    """Emit a ``task.deleted`` event for a single removed row.

    Shared chokepoint for both row-removal sites (RFC 0008 W1, closes #978):
    called from ``remove_ticket`` (``operator_remove``) and ``clear_tickets``
    (``operator_clear``), once per removed row.
    """
    # Why: emit inline under dev_queue_lock — one task.deleted per removed
    # row (not per API call). record_event nests _inbox_lock INSIDE
    # dev_queue_lock; the reverse never happens, so this is deadlock-safe.
    record_event(
        OrchestratorEventType.TASK_DELETED,
        {
            "ticket_id": removed.ticket_id,
            "client": removed.client,
            "stage": removed.stage,
            "status_at_deletion": removed.status,
            "reason": reason,
        },
        correlation_id=removed.ticket_id,
    )


def remove_ticket(ticket_id: str, client: str, *, remove_all: bool = False) -> None:
    """Remove one (or all) matching TicketTask(s) from the dev queue.

    Raises CwError when no task matches.  Raises CwError when multiple tasks
    match and *remove_all* is False.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        ]
        n = len(matches)
        if n == 0:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'."
            )
            raise CwError(msg)
        if n > 1 and not remove_all:
            msg = (
                f"Multiple dev-queue tasks ({n}) match ticket '{ticket_id}' in"
                f" client '{client}'; pass --all to remove all."
            )
            raise CwError(msg)
        match_set = {id(m) for m in matches}
        store.tasks = [t for t in store.tasks if id(t) not in match_set]
        save_dev_queue(store)
        for removed in matches:
            _emit_task_deleted(removed, "operator_remove")


def cancel_ticket(ticket_id: str, client: str) -> list[str | None]:
    """Mark a TicketTask as CANCELLED, clearing its session_id.

    Returns the list of session_ids that were cleared (one per cancelled task).
    Raises CwError when no task matches. Idempotent for already-CANCELLED tasks.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        ]
        if not matches:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'."
            )
            raise CwError(msg)
        cleared: list[str | None] = []
        changed = False
        for task in matches:
            if task.status == QueueItemStatus.CANCELLED:
                continue
            cleared.append(task.session_id)
            transition_task_status(task, QueueItemStatus.CANCELLED)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    return cleared


def cancel_task_for_session(session_id: str) -> bool:
    """Mark the RUNNING TicketTask that owns *session_id* as CANCELLED.

    Returns True if a task was cancelled, False if none matched.
    Used by _spawn_close_impl to atomically preempt the dispatcher before
    the session is marked COMPLETED. See GitHub issue #317.
    """
    with _lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.session_id == session_id and task.status == QueueItemStatus.RUNNING:
                transition_task_status(task, QueueItemStatus.CANCELLED)
                task.session_id = None
                save_dev_queue(store)
                return True
    return False


def move_ticket(ticket_id: str, client_name: str, to_lane: str) -> str:
    """Move a pending ticket to a different lane.

    Returns the previous lane name (from_lane) for event emission by the caller.

    Raises:
        CwError: if no matching task is found for (ticket_id, client_name).
        LaneNotFoundError: if to_lane is not declared for the client.
        LaneMoveError: if the task status is RUNNING, BLOCKED_ON_USER, or
            AWAITING_OPERATOR_SIGNOFF.

    Note: record_event is NOT called here — the CLI layer fires TICKET_MOVED.
    """
    with _lock():
        store = load_dev_queue()
        task = next(
            (
                t
                for t in store.tasks
                if t.ticket_id == ticket_id and t.client == client_name
            ),
            None,
        )
        if task is None:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client_name}'."
            )
            raise CwError(msg)

        client = get_client(client_name)
        declared_lane_names = [ln.name for ln in client.effective_lanes]
        if to_lane not in declared_lane_names:
            msg = (
                f"Lane '{to_lane}' is not declared for client '{client_name}'."
                f" Declared lanes: {', '.join(declared_lane_names)}."
                f" Run: cw lane add {client_name} {to_lane}"
            )
            raise LaneNotFoundError(msg)

        if task.status in _UNMOVABLE_STATUSES:
            msg = (
                f"Cannot move ticket '{ticket_id}': task is {task.status.value}."
                " Only PENDING tasks can be moved between lanes."
            )
            raise LaneMoveError(msg)

        from_lane = task.lane
        task.lane = to_lane
        save_dev_queue(store)
    return from_lane


def clear_tickets(client: str, status: QueueItemStatus | None = None) -> int:
    """Remove all TicketTasks for *client*, optionally filtered by *status*.

    Returns the number of tasks removed.
    """
    with _lock():
        store = load_dev_queue()
        if status is None:
            removed_tasks = [t for t in store.tasks if t.client == client]
        else:
            removed_tasks = [
                t for t in store.tasks if t.client == client and t.status == status
            ]
        removed_ids = {id(t) for t in removed_tasks}
        store.tasks = [t for t in store.tasks if id(t) not in removed_ids]
        save_dev_queue(store)
        for task in removed_tasks:
            _emit_task_deleted(task, "operator_clear")
    return len(removed_tasks)


def resolve_client(
    ticket_id: str,
    config: OrchestratorConfig,
    client_override: str | None,
) -> str:
    """Resolve the target client for a ticket.

    Resolution order:
    1. ``client_override`` (--client flag) if provided
    2. Prefix map: ``GEN-100`` -> prefix ``GEN`` -> client name
    3. Raise ``CwError`` if neither resolves
    """
    if client_override is not None:
        return client_override

    # Extract prefix: everything before the first '-'
    if "-" in ticket_id:
        prefix = ticket_id.split("-", maxsplit=1)[0]
        if prefix in config.linear_prefix_map:
            return config.linear_prefix_map[prefix]

    msg = (
        f"Cannot resolve client for ticket '{ticket_id}'."
        " Use --client to specify, or add the prefix to linear_prefix_map in"
        " ~/.claude-workspace/orchestrator.yaml."
    )
    raise CwError(msg)


def list_tickets(client: str | None = None) -> list[TicketTask]:
    """Return tickets from the dev queue, optionally filtered by client."""
    store = load_dev_queue()
    if client is None:
        return list(store.tasks)
    return [t for t in store.tasks if t.client == client]


def _newest_by_created_at(tasks: list[TicketTask]) -> TicketTask:
    """Return the task with the newest created_at (duplicate-row tie-break).

    Callers must pass a non-empty list (raises via max() on empty by design;
    every existing call site already guards emptiness).
    """
    return max(tasks, key=lambda t: t.created_at)


def _find_ticket(store: DevQueueStore, ticket_id: str, client: str) -> TicketTask:
    """Return the TicketTask matching (ticket_id, client) or raise CwError.

    Selection priority: PENDING/RUNNING (live, newest created_at) →
    BLOCKED_ON_USER (newest created_at) → terminal (newest created_at).
    Re-resolved on every call — callers that poll (e.g. dev_queue_wait)
    pick up re-enqueued tasks on the next tick automatically.

    Emits a one-time stderr warning when multiple live PENDING/RUNNING tasks
    exist for the same (ticket_id, client).
    """
    # Why: add-after-terminal creates duplicate (client, ticket_id) rows.
    # Returning the oldest terminal record would cause wait to resolve
    # immediately on a stale status while a fresh RUNNING task is live.
    import click

    matches = [
        t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
    ]
    if not matches:
        msg = f"No dev-queue task found for ticket '{ticket_id}' in client '{client}'."
        raise CwError(msg)

    live = [
        t
        for t in matches
        if t.status in {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    ]
    if live:
        if len(live) > 1:
            click.echo(
                f"Warning: {len(live)} live tasks for ticket '{ticket_id}' "
                f"in client '{client}'; binding to newest.",
                err=True,
            )
        return _newest_by_created_at(live)

    blocked = [t for t in matches if t.status in _APPROVABLE_STATUSES]
    if blocked:
        return _newest_by_created_at(blocked)

    return _newest_by_created_at(matches)
