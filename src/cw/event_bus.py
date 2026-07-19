"""Generic event-bus core for the channel servers.

``EventBus`` owns the append/subscribe/broadcast/cursor machinery that was
duplicated across the channel servers (``cw_queue_events_server`` migrated
first, #1303; the PR and operator servers are follow-up tickets). It is a
direct extraction of those module-level functions with ``self.`` state — the
lock nesting, fsync durability, and #433 TOCTOU ordering are preserved exactly.

Path resolution is deliberately deferred to call time: ``__init__`` stores only
the file *names*, and every method recomputes ``state_dir() / self.events_file``
(or ``self.cursors_file``) inline, so the autouse test fixture that monkeypatches
``cw.config.STATE_DIR`` per-test is honoured (R4). The two locks
(``self.lock`` for the subscriber list, ``self.file_lock`` for the on-disk log
and cursors) are separate, non-reentrant ``threading.Lock`` instances and must
never be collapsed into one or promoted to an ``RLock`` (R5).

Attributes and methods are intentionally PUBLIC (no leading underscore): the
server module binds its historic underscored module-level names to them as
plain public-attribute reads (e.g. ``_lock = _bus.lock``), which keeps every
existing import site and in-place test mutation working without tripping ruff's
SLF001 private-member-access rule (R2).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
from typing import Any

from cw.atomic import atomic_write_text
from cw.config import state_dir

logger = logging.getLogger(__name__)


class EventBus:
    """Append/subscribe/broadcast/cursor core shared by the channel servers."""

    def __init__(
        self,
        *,
        events_file: str,
        cursors_file: str,
        log_label: str,
    ) -> None:
        self.events_file = events_file
        self.cursors_file = cursors_file
        self.log_label = log_label

        self.lock = threading.Lock()
        self.subscribers: list[queue.SimpleQueue[dict[str, Any]]] = []

        self.file_lock = threading.Lock()
        self.cursors: dict[str, int] = {}
        self.event_offset: list[int] = [0]

    def subscribe(self) -> queue.SimpleQueue[dict[str, Any]]:
        """Register a subscriber queue and return it."""
        q: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        with self.lock:
            self.subscribers.append(q)
        logger.info(
            "%s subscriber added, total=%d", self.log_label, len(self.subscribers)
        )
        return q

    def unsubscribe(self, q: queue.SimpleQueue[dict[str, Any]]) -> None:
        """Remove a subscriber queue."""
        with self.lock, contextlib.suppress(ValueError):
            self.subscribers.remove(q)
        logger.info(
            "%s subscriber removed, total=%d", self.log_label, len(self.subscribers)
        )

    def append_event(self, notification: dict[str, Any]) -> None:
        """Persist notification to the events file with a monotonic offset."""
        with self.file_lock:
            path = state_dir() / self.events_file
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {**notification, "offset": self.event_offset[0]}
            with path.open("a") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.event_offset[0] += 1

    def read_events_from_offset_locked(self, from_offset: int) -> list[dict[str, Any]]:
        """Read events with offset >= from_offset. Caller MUST hold ``file_lock``.

        The non-re-entrant read core. ``subscribe_with_cursor`` calls this while
        already holding ``file_lock`` (the public wrapper below would deadlock —
        ``file_lock`` is a plain, non-reentrant ``threading.Lock``).
        """
        path = state_dir() / self.events_file
        if not path.exists():
            return []
        results: list[dict[str, Any]] = []
        for raw_line in path.read_text().splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record: dict[str, Any] = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning(
                    "%s: skipping malformed line: %r", self.events_file, stripped
                )
                continue
            if record.get("offset", -1) >= from_offset:
                results.append(record)
        return results

    def read_events_from_offset(self, from_offset: int) -> list[dict[str, Any]]:
        """Read all events with offset >= from_offset from the events file."""
        with self.file_lock:
            return self.read_events_from_offset_locked(from_offset)

    def load_cursors(self) -> dict[str, int]:
        """Load per-subscriber cursors from disk."""
        with self.file_lock:
            path = state_dir() / self.cursors_file
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    logger.warning("%s: unexpected shape, ignoring", self.cursors_file)
                    return {}
                return {str(k): int(v) for k, v in data.items()}
            except json.JSONDecodeError:
                logger.warning("%s: corrupt, ignoring", self.cursors_file)
                return {}

    def load_offset_from_file(self) -> int:
        """Determine current offset from the events file on disk."""
        with self.file_lock:
            path = state_dir() / self.events_file
            if not path.exists():
                return 0
            max_offset = -1
            for raw_line in path.read_text().splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    record: dict[str, Any] = json.loads(stripped)
                    offset = record.get("offset", -1)
                    if isinstance(offset, int) and offset > max_offset:
                        max_offset = offset
                except json.JSONDecodeError:
                    continue
            return max_offset + 1

    def subscribe_with_cursor(
        self, client_id: str
    ) -> queue.SimpleQueue[dict[str, Any]]:
        """Subscribe and replay any missed events since the client's last cursor.

        TOCTOU fix (#433): register the subscriber AND read the replay backlog
        while holding ``file_lock``, so no ``broadcast()`` can append+fan-out in
        the gap. ``append_event`` takes ``file_lock`` to persist, so while we
        hold it no new event can be appended; any event appended after we
        release is therefore delivered exactly once via the live fan-out path to
        the now-registered queue — never lost (the earlier snapshot-then-subscribe
        ordering could drop the boundary event) and never duplicated (it is not
        in the backlog we replay).
        """
        with self.file_lock:
            cursor = self.cursors.get(client_id, 0)
            q = self.subscribe()
            missed = self.read_events_from_offset_locked(cursor)
            for record in missed:
                q.put_nowait(record)
        return q

    def ack_offset(self, client_id: str, offset: int) -> None:
        """Advance the per-subscriber cursor and persist to disk."""
        with self.file_lock:
            self.cursors[client_id] = offset
            path = state_dir() / self.cursors_file
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(self.cursors))

    def broadcast(self, notification: dict[str, Any]) -> None:
        """Fan-out notification to all subscriber queues."""
        self.append_event(notification)  # FIRST: persist (file_lock only)
        with self.lock:  # THEN: get subscribers
            subs = list(self.subscribers)
        for s in subs:
            s.put_nowait(notification)
        logger.debug("%s broadcast to %d subscribers", self.log_label, len(subs))
