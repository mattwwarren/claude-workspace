"""Record a snapshot: store write, event, then the row's digest, in that order.

Order (RFC 0019 R13): the fetch happens before this module is called, with no
lock held. The store write takes no lock. Then, under ``dev_queue_lock`` (the
one bounded literal call, allowlisted in ``tests/test_config.py``), the queue
is loaded, the row is resolved, the event is recorded FIRST, and the two row
fields are set and saved. Rendering is left to the caller.

Observability: the event and the WARNING log take ``ticket_id`` and ``client``
from the validated snapshot, and ``comments`` is its length. No ticket text.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.ticket_snapshot._fetch import snapshot_from_github
from cw.ticket_snapshot._models import TicketSnapshot
from cw.ticket_snapshot._store import write_snapshot

_log = logging.getLogger("cw.ticket_snapshot")


@dataclass(frozen=True)
class SnapshotRecord:
    """What a recorded snapshot did. ``row_updated`` is False when no row matched."""

    ticket_id: str
    client: str
    snapshot_digest: str
    previous_digest: str | None
    refresh: bool
    comments: int
    row_updated: bool


def record_snapshot(
    snapshot: TicketSnapshot, *, refresh: bool = False
) -> SnapshotRecord:
    """Write the store, record the event, and stamp the row when one matches."""
    digest = write_snapshot(snapshot)
    with dev_queue_lock(bounded=True):
        store = load_dev_queue()
        try:
            task = _find_ticket(store, snapshot.ticket_id, snapshot.client, warn=False)
        except CwError:
            task = None
        previous = task.snapshot_digest if task is not None else None
        comments = len(snapshot.comments)
        payload: dict[str, object] = {
            "ticket_id": snapshot.ticket_id,
            "client": snapshot.client,
            "snapshot_digest": digest,
            "previous_digest": previous,
            "refresh": refresh,
            "comments": comments,
            "row_updated": task is not None,
        }
        record_event(
            OrchestratorEventType.TICKET_SNAPSHOT_RECORDED,
            payload,
            correlation_id=snapshot.ticket_id,
        )
        if task is not None:
            task.snapshot_digest = digest
            task.snapshot_fetched_at = snapshot.fetched_at
            save_dev_queue(store)
    _log.warning(
        "ticket snapshot recorded ticket_id=%s client=%s digest=%s comments=%d"
        " row_updated=%s",
        snapshot.ticket_id,
        snapshot.client,
        digest,
        comments,
        task is not None,
    )
    return SnapshotRecord(
        ticket_id=snapshot.ticket_id,
        client=snapshot.client,
        snapshot_digest=digest,
        previous_digest=previous,
        refresh=refresh,
        comments=comments,
        row_updated=task is not None,
    )


def record_snapshot_from_github(
    ticket_id: str,
    client: str,
    cwd: Path,
    *,
    refresh: bool = False,
) -> SnapshotRecord:
    """Fetch the GitHub issue live, then record it. Delegates; holds no lock itself."""
    snapshot = snapshot_from_github(ticket_id, client, cwd)
    return record_snapshot(snapshot, refresh=refresh)
