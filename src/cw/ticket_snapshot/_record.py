"""Record a snapshot: store write, event, then the row's digest, in that order.

Order (RFC 0019 R13): the fetch happens before this module is called, with no
lock held. The store write takes no lock. Then, under ``dev_queue_lock`` (the
one bounded literal call, allowlisted in ``tests/test_config.py``), the queue
is loaded, the row is resolved, the event is recorded FIRST, and the two row
fields are set and saved. Rendering is left to the caller.

Observability: the event and the WARNING log take ``ticket_id`` and ``client``
from the validated snapshot, and ``comments`` is its length. No ticket text.

Author conflicts (RFC 0019 T2 R5, #3712): with ``detect_author_conflicts``
(the model-reported stdin path only; GitHub never flags) the step first reads
the row's previous snapshot, unlocked, and flags every comment whose id was
reported with a different non-null ``author_id``. A newly set flag records one
``ticket.snapshot_author_conflict`` event after the lock is released.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.ticket_snapshot._fetch import snapshot_from_github
from cw.ticket_snapshot._models import (
    SnapshotComment,
    SnapshotUntrustedError,
    TicketSnapshot,
)
from cw.ticket_snapshot._store import read, write_snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

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


def current_row(ticket_id: str, client: str) -> TicketTask | None:
    """The queue row as it is now, read without the lock; None when absent.

    ``save_dev_queue`` writes atomically, so this sees the old or the new file,
    never a torn one. The same unlocked shape as ``cw.ticket_view``.
    """
    try:
        return _find_ticket(load_dev_queue(), ticket_id, client, warn=False)
    except CwError:
        return None
    except (OSError, ValueError) as exc:
        # Never-raise contract: a queue-store read can fail with OSError or a
        # JSON decode error (ValueError). Log the class only, never its text.
        _log.warning(
            "ticket snapshot current row unreadable; degrading to None"
            " ticket_id=%s client=%s error=%s",
            ticket_id,
            client,
            type(exc).__name__,
        )
        return None


def _previous_snapshot(snapshot: TicketSnapshot) -> TicketSnapshot | None:
    """The row's stored snapshot before this record, or None (no comparison)."""
    row = current_row(snapshot.ticket_id, snapshot.client)
    if row is None:
        return None
    try:
        return read(row)
    except SnapshotUntrustedError as exc:
        _log.warning(
            "ticket snapshot previous unreadable; author conflicts not compared"
            " ticket_id=%s client=%s reason=%s",
            snapshot.ticket_id,
            snapshot.client,
            exc.reason,
        )
        return None
    except OSError as exc:
        _log.warning(
            "ticket snapshot previous unreadable; author conflicts not compared"
            " ticket_id=%s client=%s error=%s",
            snapshot.ticket_id,
            snapshot.client,
            type(exc).__name__,
        )
        return None


def _author_ids(comments: list[SnapshotComment]) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = {}
    for comment in comments:
        if comment.author_id is not None:
            ids.setdefault(comment.id, set()).add(comment.author_id.casefold())
    return ids


def flag_author_conflicts(
    snapshot: TicketSnapshot, previous: TicketSnapshot | None
) -> tuple[TicketSnapshot, int]:
    """Return *snapshot* with ``author_conflict`` set, and the newly set count.

    A comment id reported with two different non-null casefolded
    ``author_id`` values, in *previous* or within *snapshot*, is flagged. A
    flag in *previous* carries forward. ``None`` to a value is not a conflict.
    """
    earlier = previous.comments if previous is not None else []
    carried = {comment.id for comment in earlier if comment.author_conflict}
    seen = _author_ids(earlier)
    for comment_id, values in _author_ids(snapshot.comments).items():
        seen.setdefault(comment_id, set()).update(values)
    flagged: list[SnapshotComment] = []
    newly = 0
    for comment in snapshot.comments:
        conflict = len(seen.get(comment.id, ())) > 1
        if conflict and comment.id not in carried and not comment.author_conflict:
            newly += 1
        if conflict or comment.id in carried or comment.author_conflict:
            flagged.append(comment.model_copy(update={"author_conflict": True}))
        else:
            flagged.append(comment)
    return snapshot.model_copy(update={"comments": flagged}), newly


def _emit_author_conflict(snapshot: TicketSnapshot, digest: str, newly: int) -> None:
    """Record ``ticket.snapshot_author_conflict``; best-effort, never raises."""
    _log.warning(
        "ticket snapshot author conflict ticket_id=%s client=%s digest=%s comments=%d",
        snapshot.ticket_id,
        snapshot.client,
        digest,
        newly,
    )
    try:
        record_event(
            OrchestratorEventType.TICKET_SNAPSHOT_AUTHOR_CONFLICT,
            {
                "ticket_id": snapshot.ticket_id,
                "client": snapshot.client,
                "snapshot_digest": digest,
                "comments": newly,
            },
            correlation_id=snapshot.ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "ticket.snapshot_author_conflict event not recorded ticket_id=%s",
            snapshot.ticket_id,
        )


def record_snapshot(
    snapshot: TicketSnapshot,
    *,
    refresh: bool = False,
    detect_author_conflicts: bool = False,
) -> SnapshotRecord:
    """Write the store, record the event, and stamp the row when one matches."""
    newly = 0
    if detect_author_conflicts:
        snapshot, newly = flag_author_conflicts(snapshot, _previous_snapshot(snapshot))
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
    if newly:
        _emit_author_conflict(snapshot, digest, newly)
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
