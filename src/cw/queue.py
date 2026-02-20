"""Queue management for task delegation and daemon processing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import QUEUES_DIR
from cw.models import QueueItem, QueueItemStatus, QueueStore, TaskSpec

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import SessionPurpose


def _queue_path(client: str) -> Path:
    return QUEUES_DIR / f"{client}.json"


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
    store = load_queue(client)
    item = QueueItem(client=client, task=task)
    store.items.append(item)
    save_queue(client, store)
    return item


def claim_next(
    client: str,
    purpose: SessionPurpose | None = None,
) -> QueueItem | None:
    """Atomically claim the next pending item, optionally filtered by purpose.

    Returns the claimed item (now RUNNING), or None if queue is empty.
    """
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
    store = load_queue(client)
    item = store.find_item(item_id)
    if item is None:
        msg = f"Queue item not found: {item_id}"
        raise ValueError(msg)
    item.status = QueueItemStatus.COMPLETED
    item.completed_at = datetime.now(UTC)
    item.result = result
    save_queue(client, store)


def fail_item(client: str, item_id: str, error: str) -> None:
    """Mark an item as failed with an error message."""
    store = load_queue(client)
    item = store.find_item(item_id)
    if item is None:
        msg = f"Queue item not found: {item_id}"
        raise ValueError(msg)
    item.status = QueueItemStatus.FAILED
    item.completed_at = datetime.now(UTC)
    item.result = error
    save_queue(client, store)


def remove_item(client: str, item_id: str) -> None:
    """Delete an item from the queue."""
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
