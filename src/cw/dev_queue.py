"""Dev-queue management for orchestrator ticket dispatch."""

from __future__ import annotations

import contextlib
import fcntl
import json
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.config import (
    dev_plan_file,
    dev_plan_lock,
    dev_queue_file,
)
from cw.config import (
    dev_queue_lock as _dev_queue_lock_file,
)
from cw.exceptions import CwError
from cw.models import (
    DevQueueStore,
    DispatchPlan,
    OrchestratorConfig,
    QueueItemStatus,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dev queue."""
    dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
    fd = _dev_queue_lock_file().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# Public alias for callers that need the dev-queue lock directly (e.g. the
# reconciler, which needs load → mutate → save around a RUNNING→PENDING
# revert). Prefer higher-level helpers like ``add_ticket`` when available.
dev_queue_lock = _lock


@contextlib.contextmanager
def _plan_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dispatch plan."""
    dev_plan_file().parent.mkdir(parents=True, exist_ok=True)
    fd = dev_plan_lock().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def plan_path() -> Path:
    """Return the path to the persisted dispatch plan file."""
    return dev_plan_file()


def save_plan(plan: DispatchPlan) -> Path:
    """Persist a DispatchPlan to disk under the plan file lock.

    Returns the path the plan was written to.
    """
    with _plan_lock():
        path = dev_plan_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, plan.model_dump_json(indent=2))
    return path


def load_plan() -> DispatchPlan | None:
    """Load the persisted DispatchPlan, returning None if missing.

    Returns None if the plan file does not exist or fails validation.
    Does not raise on validation errors — callers should fall back to
    enqueue order when None is returned.
    """
    path = dev_plan_file()
    if not path.exists():
        return None
    try:
        return DispatchPlan.model_validate_json(path.read_text())
    except (ValueError, OSError):
        return None


def load_dev_queue() -> DevQueueStore:
    """Load the dev queue from disk, returning an empty store if missing."""
    path = dev_queue_file()
    if not path.exists():
        return DevQueueStore()
    raw = json.loads(path.read_text())
    return DevQueueStore.model_validate(raw)


def save_dev_queue(store: DevQueueStore) -> None:
    """Persist the dev queue to disk atomically."""
    path = dev_queue_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, store.model_dump_json(indent=2))


def add_ticket(task: TicketTask) -> bool:
    """Enqueue a TicketTask, acquiring the file lock atomically.

    Returns True if the task was inserted, False if a task with the same
    (client, ticket_id) is already PENDING or RUNNING (deduplication guard).
    """
    _active = {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    with _lock():
        store = load_dev_queue()
        for existing in store.tasks:
            if (
                existing.client == task.client
                and existing.ticket_id == task.ticket_id
                and existing.status in _active
            ):
                return False
        store.tasks.append(task)
        save_dev_queue(store)
    return True


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


def cancel_ticket(ticket_id: str, client: str) -> None:
    """Mark a TicketTask as CANCELLED, clearing its session_id.

    Raises CwError when no task matches (ticket_id + client).  Already-CANCELLED
    tasks are silently skipped (idempotent).  Acquires the file lock atomically.
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
        changed = False
        for task in matches:
            if task.status == QueueItemStatus.CANCELLED:
                continue
            task.status = QueueItemStatus.CANCELLED
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)


def clear_tickets(client: str, status: QueueItemStatus | None = None) -> int:
    """Remove all TicketTasks for *client*, optionally filtered by *status*.

    Returns the number of tasks removed.
    """
    with _lock():
        store = load_dev_queue()
        if status is None:
            kept = [t for t in store.tasks if t.client != client]
        else:
            kept = [
                t
                for t in store.tasks
                if not (t.client == client and t.status == status)
            ]
        removed = len(store.tasks) - len(kept)
        store.tasks = kept
        save_dev_queue(store)
    return removed


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
