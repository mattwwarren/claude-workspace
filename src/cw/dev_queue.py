"""Dev-queue management for orchestrator ticket dispatch."""

from __future__ import annotations

import contextlib
import fcntl
import json
from typing import TYPE_CHECKING

from cw.config import DEV_QUEUE_FILE, DEV_QUEUE_LOCK
from cw.exceptions import CwError
from cw.models import DevQueueStore, OrchestratorConfig, TicketTask

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dev queue."""
    DEV_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = DEV_QUEUE_LOCK.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def load_dev_queue() -> DevQueueStore:
    """Load the dev queue from disk, returning an empty store if missing."""
    if not DEV_QUEUE_FILE.exists():
        return DevQueueStore()
    raw = json.loads(DEV_QUEUE_FILE.read_text())
    return DevQueueStore.model_validate(raw)


def save_dev_queue(store: DevQueueStore) -> None:
    """Persist the dev queue to disk."""
    DEV_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEV_QUEUE_FILE.write_text(store.model_dump_json(indent=2))


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
