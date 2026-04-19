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
    dev_queue_lock,
)
from cw.exceptions import CwError
from cw.models import DevQueueStore, DispatchPlan, OrchestratorConfig, TicketTask

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dev queue."""
    dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
    fd = dev_queue_lock().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


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


def add_ticket(task: TicketTask) -> None:
    """Enqueue a TicketTask, acquiring the file lock atomically."""
    with _lock():
        store = load_dev_queue()
        store.tasks.append(task)
        save_dev_queue(store)


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
