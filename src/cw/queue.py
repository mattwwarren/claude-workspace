"""Queue management for task delegation and daemon processing."""

from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import QUEUES_DIR
from cw.history import EventType, HistoryEvent, record_event
from cw.models import QueueItem, QueueItemStatus, QueueStore, TaskSpec

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cw.models import SessionPurpose


def _queue_path(client: str) -> Path:
    return QUEUES_DIR / f"{client}.json"


def _lock_path(client: str) -> Path:
    return QUEUES_DIR / f".{client}.lock"


@contextlib.contextmanager
def _queue_lock(client: str) -> Iterator[None]:
    """Acquire an exclusive file lock for a client's queue."""
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(client)
    fd = lock.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def load_queue(client: str) -> QueueStore:
    """Load the queue for a client from disk."""
    path = _queue_path(client)
    if not path.exists():
        return QueueStore()
    raw = json.loads(path.read_text())
    return QueueStore.model_validate(raw)


def save_queue(client: str, store: QueueStore) -> None:
    """Persist a client's queue to disk."""
    path = _queue_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.model_dump_json(indent=2))


def add_item(client: str, task: TaskSpec) -> QueueItem:
    """Add a pending item to the client's queue."""
    with _queue_lock(client):
        store = load_queue(client)
        item = QueueItem(client=client, task=task)
        store.items.append(item)
        save_queue(client, store)
    record_event(client, HistoryEvent(
        event_type=EventType.QUEUE_ITEM_ADDED,
        client=client,
        purpose=task.purpose,
        detail=task.description,
        metadata={"queue_item_id": item.id},
    ))
    return item


def claim_next(
    client: str,
    purpose: SessionPurpose | None = None,
) -> QueueItem | None:
    """Claim the next pending item, optionally filtered by purpose.

    Returns the claimed item (now RUNNING), or None if queue is empty.

    Note: This uses file-level locking (``fcntl.flock``) to prevent
    concurrent processes from claiming the same item.  When *purpose*
    is ``None``, any pending item may be claimed regardless of its
    target purpose.
    """
    with _queue_lock(client):
        store = load_queue(client)
        for item in store.items:
            if item.status != QueueItemStatus.PENDING:
                continue
            if purpose is not None and item.task.purpose != purpose:
                continue
            item.status = QueueItemStatus.RUNNING
            item.started_at = datetime.now(UTC)
            save_queue(client, store)
            return item
    return None


def complete_item(client: str, item_id: str, result: str) -> None:
    """Mark an item as completed with a result summary."""
    with _queue_lock(client):
        store = load_queue(client)
        item = store.find_item(item_id)
        if item is None:
            msg = f"Queue item not found: {item_id}"
            raise ValueError(msg)
        item.status = QueueItemStatus.COMPLETED
        item.completed_at = datetime.now(UTC)
        item.result = result
        save_queue(client, store)
    record_event(client, HistoryEvent(
        event_type=EventType.QUEUE_ITEM_COMPLETED,
        client=client,
        detail=result,
        metadata={"queue_item_id": item_id},
    ))


def fail_item(client: str, item_id: str, error: str) -> None:
    """Mark an item as failed with an error message."""
    with _queue_lock(client):
        store = load_queue(client)
        item = store.find_item(item_id)
        if item is None:
            msg = f"Queue item not found: {item_id}"
            raise ValueError(msg)
        item.status = QueueItemStatus.FAILED
        item.completed_at = datetime.now(UTC)
        item.result = error
        save_queue(client, store)
    record_event(client, HistoryEvent(
        event_type=EventType.QUEUE_ITEM_FAILED,
        client=client,
        detail=error,
        metadata={"queue_item_id": item_id},
    ))


def remove_item(client: str, item_id: str) -> None:
    """Delete an item from the queue."""
    with _queue_lock(client):
        store = load_queue(client)
        store.items = [i for i in store.items if i.id != item_id]
        save_queue(client, store)


def clear_queue(
    client: str,
    *,
    purpose: SessionPurpose | None = None,
    status: QueueItemStatus | None = None,
) -> int:
    """Bulk clear items from the queue, returning the count removed.

    Filters by purpose and/or status if provided.
    """
    with _queue_lock(client):
        store = load_queue(client)
        original_count = len(store.items)
        store.items = [
            i
            for i in store.items
            if not (
                (purpose is None or i.task.purpose == purpose)
                and (status is None or i.status == status)
            )
        ]
        removed = original_count - len(store.items)
        save_queue(client, store)
    return removed
