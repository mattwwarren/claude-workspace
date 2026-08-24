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

``load_offset_from_file`` determines the current offset via a bounded reverse
read from EOF rather than parsing the whole channel log (#1986).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
from typing import TYPE_CHECKING, Any, BinaryIO

from cw.atomic import atomic_write_text
from cw.config import state_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

_REVERSE_READ_CHUNK_BYTES = 65536  # 64 KiB per backward seek


def _read_reverse_chunk(f: BinaryIO, end: int, size: int) -> bytes:
    """Read up to ``size`` bytes of ``f`` ending at byte offset ``end``.

    A thin, separately-nameable wrapper around the seek+read pair so tests can
    monkeypatch it to count bytes actually pulled off disk (the low-level I/O
    primitive ``_iter_lines_reverse`` drives in its backward walk).
    """
    start = max(0, end - size)
    f.seek(start)
    return f.read(end - start)


def _decode_reverse_candidate(raw: bytes) -> str | None:
    """Decode+strip one candidate line; ``None`` if blank or undecodable.

    A crash mid-``append_event`` can truncate the trailing line mid multi-byte
    UTF-8 character, which raises ``UnicodeDecodeError`` here — well before
    ``json.loads`` would ever see it. Treated the same as a malformed line:
    warn and let the caller keep walking backward.
    """
    if not raw.strip():
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning("skipping undecodable line: %r", raw)
        return None
    return text or None


def _iter_line_groups_reverse(path: Path) -> Iterator[list[str]]:
    """Yield groups of decoded, non-blank lines from ``path``, EOF to BOF.

    Each group is exactly the complete lines recovered from one backward
    ``_REVERSE_READ_CHUNK_BYTES`` read (or the final BOF flush) — no extra
    disk I/O is needed to produce a group beyond what was already pulled. A
    line spanning a chunk boundary is reassembled via a carry buffer and
    attributed to the group in which its leading boundary is found.
    """
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        carry = b""
        while pos > 0:
            chunk = _read_reverse_chunk(f, pos, _REVERSE_READ_CHUNK_BYTES)
            pos -= len(chunk)
            data = chunk + carry
            raw_lines = data.split(b"\n")
            carry = raw_lines[0]
            group = [
                line
                for raw in reversed(raw_lines[1:])
                if (line := _decode_reverse_candidate(raw)) is not None
            ]
            if group:
                yield group
        line = _decode_reverse_candidate(carry)
        if line is not None:
            yield [line]


def _last_offset_from_reverse_scan(path: Path, events_file: str) -> int | None:
    """Return the max valid offset within the first non-empty read group.

    ``append_event`` assigns offsets under ``file_lock``, but that lock is a
    ``threading.Lock`` — thread-scoped, not process-scoped. Two processes
    appending to the same channel log (e.g. a lingering server during a
    crash-restart) can interleave writes so the file is *not* offset-monotonic
    in file position, even though each process's own writes are. Returning the
    first valid offset found walking backward can therefore return a value
    lower than the true max, which would make the next ``append_event`` reuse
    an offset already on disk and replay events to subscribers.

    # Why: take the max over the whole group already read off disk, not the
    # first record found in it — this stays bounded (no extra I/O; the group
    # is already in memory) while staying correct under the interleaving
    # above. Do not "simplify" this back to first-found (#1986 round 2).

    Returns ``None`` if BOF is reached with no valid record.
    """
    for group in _iter_line_groups_reverse(path):
        offsets: list[int] = []
        for line in group:
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("%s: skipping malformed line: %r", events_file, line)
                continue
            offset = record.get("offset")
            if isinstance(offset, int):
                offsets.append(offset)
        if offsets:
            return max(offsets)
    return None


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
        """Determine current offset via a bounded reverse read from EOF (#1986).

        Rather than parsing the whole channel log to compute max(offset) + 1,
        this walks backward from EOF in ``_REVERSE_READ_CHUNK_BYTES`` chunks,
        stopping as soon as one chunk yields any valid record and returning
        the max offset within that chunk — see
        ``_last_offset_from_reverse_scan`` for why the max over the read
        chunk, not the first record found, is required for correctness.
        """
        with self.file_lock:
            path = state_dir() / self.events_file
            if not path.exists():
                return 0
            found = _last_offset_from_reverse_scan(path, self.events_file)
            return found + 1 if found is not None else 0

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
