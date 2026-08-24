"""Orchestrator event bus: append-only inbox with cursor-based consumption."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, ValidationError

from cw.atomic import atomic_write_text
from cw.config import events_dir, load_orchestrator_config
from cw.exceptions import CwError
from cw.models import OrchestratorConfig, OrchestratorEvent, OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


def inbox_path() -> Path:
    """Return the path to the global event inbox JSONL file."""
    return events_dir() / "inbox.jsonl"


def _cursor_path(consumer: str) -> Path:
    """Return the cursor file path for a named consumer."""
    return events_dir() / "cursors" / f"{consumer}.json"


def _lock_path() -> Path:
    """Return the lock file path for the event inbox."""
    return events_dir() / ".inbox.lock"


@contextlib.contextmanager
def _inbox_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the event inbox."""
    events_dir().mkdir(parents=True, exist_ok=True)
    lock = _lock_path()
    fd = lock.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def record_event(
    event_type: OrchestratorEventType,
    payload: dict[str, Any] | None = None,
    *,
    correlation_id: str | None = None,
) -> OrchestratorEvent:
    """Append a new event to the inbox and return it.

    May trigger an in-band auto-prune (#1980): once the inbox exceeds
    ``event_inbox_retention_bytes``, this call also rewrites the inbox down
    to the newest ``event_inbox_retention_count`` events before returning,
    under the same ``_inbox_lock`` acquisition. See
    :func:`_maybe_auto_prune_locked` for the trigger and
    :func:`_prune_events_locked` for the composition contract with
    ``_poll_follow_state``'s inode-change detection.

    Args:
        event_type: The orchestrator event type.
        payload: Arbitrary JSON-serialisable payload dict.
        correlation_id: Optional ID linking related events.

    Returns:
        The newly created and persisted event.
    """
    event = OrchestratorEvent(
        type=event_type,
        payload=payload or {},
        correlation_id=correlation_id,
    )
    with _inbox_lock():
        inbox = inbox_path()
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a") as f:
            f.write(event.model_dump_json() + "\n")
            size = f.tell()
        _maybe_auto_prune_locked(size)
    return event


class _AutoPruneConfigCache:
    """Mutable holder for the cached config.

    A plain pair of module-level scalars would need ``global`` to update
    (PLW0603, disallowed by this repo's zero-violation ruff gate) -- mutating
    attributes on a module-level singleton instance instead avoids reassigning
    the module-level name itself.
    """

    def __init__(self) -> None:
        self.config: OrchestratorConfig | None = None
        self.loaded_at: float = 0.0


_AUTO_PRUNE_CONFIG_CACHE = _AutoPruneConfigCache()
_AUTO_PRUNE_CONFIG_TTL_SECONDS = 30.0  # matches tick_interval_seconds's default cadence


def _load_auto_prune_config_cached() -> OrchestratorConfig:
    """Return the cached OrchestratorConfig, reloading at most once per TTL.

    Mirrors ``_run_poller``'s ``last_good_config`` fallback shape
    (``cw_queue_events_server.py:302-325``) exactly: on a load failure, reuse
    the last successfully-parsed config rather than raising, because
    ``record_event`` has 114+ call sites and must never fail because a config
    file was momentarily unparseable (GitHub #1980, "Ambiguity Resolutions --
    round 1", Q2). On a cold start with no cache populated yet, fall back to
    ``OrchestratorConfig()`` safe defaults, exactly as ``_run_poller`` falls
    back before its first successful load.
    """
    cache = _AUTO_PRUNE_CONFIG_CACHE
    now = time.monotonic()
    if cache.config is None or now - cache.loaded_at > _AUTO_PRUNE_CONFIG_TTL_SECONDS:
        try:
            cache.config = load_orchestrator_config()
            cache.loaded_at = now
        except Exception:
            # Why: a config reload failure must never fail record_event,
            # which has 114+ call sites -- fall back to the last config that
            # loaded successfully (or safe defaults, pre-first-load),
            # mirroring _run_poller's last_good_config precedent exactly. Do
            # NOT advance cache.loaded_at here: unlike _run_poller (which
            # retries every tick unconditionally), this cache is TTL-gated,
            # so leaving the timestamp alone makes the very next call retry
            # the load immediately rather than being stuck on a stale/absent
            # cache for the full TTL.
            logger.exception("auto-prune config reload failed, using last-known-good")
    return cache.config if cache.config is not None else OrchestratorConfig()


def _maybe_auto_prune_locked(size_bytes: int) -> None:
    """Prune the inbox in-band if it has grown past the configured threshold.

    Called from record_event while _inbox_lock is already held. Best-effort:
    a failure here must not prevent the event that was just appended from
    being considered recorded, so failures are logged and swallowed.
    """
    config = _load_auto_prune_config_cached()
    if not config.event_inbox_auto_prune_enabled:
        return
    if size_bytes <= config.event_inbox_retention_bytes:
        return
    try:
        result = _prune_events_locked(
            before=None, keep=config.event_inbox_retention_count, archive=True
        )
    except Exception:
        logger.exception("auto-prune of event inbox failed; continuing")
        return
    logger.info(
        "auto-pruned event inbox: archived=%d kept=%d archive_path=%s",
        result.archived_count,
        result.kept_count,
        result.archive_path,
    )


class PruneResult(BaseModel):
    """Outcome of a :func:`prune_events` call."""

    archived_count: int
    deleted_count: int
    archive_path: str | None
    kept_count: int


def _archive_path_for_today() -> Path:
    """Return today's inbox archive path: ``events/inbox.<YYYY-MM-DD>.jsonl``."""
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    return events_dir() / f"inbox.{date_str}.jsonl"


def _partition_events_by_before(
    events: list[OrchestratorEvent], before: datetime
) -> tuple[list[OrchestratorEvent], list[OrchestratorEvent]]:
    """Split *events* into (kept, pruned) by created_at against *before*."""
    kept = [e for e in events if e.created_at >= before]
    pruned = [e for e in events if e.created_at < before]
    return kept, pruned


def _partition_events_by_keep(
    events: list[OrchestratorEvent], keep: int
) -> tuple[list[OrchestratorEvent], list[OrchestratorEvent]]:
    """Split *events* into (kept, pruned), keeping the newest *keep* events."""
    if keep <= 0:
        return [], list(events)
    if keep >= len(events):
        return list(events), []
    split = len(events) - keep
    return events[split:], events[:split]


def prune_events(
    *,
    before: datetime | None = None,
    keep: int | None = None,
    archive: bool = True,
) -> PruneResult:
    """Truncate the inbox by age or count, archiving (default) or deleting the rest.

    Exactly one of *before* or *keep* must be given: *before* prunes every
    event with ``created_at`` earlier than the cutoff; *keep* retains the
    newest *keep* events and prunes the rest.

    When *archive* is True (default), pruned events are appended to
    ``events/inbox.<YYYY-MM-DD>.jsonl`` (plain JSONL, no compression) before
    being dropped from the inbox. When False, pruned events are discarded
    without being written anywhere.

    The read, rewrite, and (optional) archive-append all happen under a
    single acquisition of the inbox lock — ``_inbox_lock`` is not reentrant,
    so releasing and reacquiring it mid-call would self-deadlock.

    Deliberately does not call :func:`record_event`: doing so would require
    a second, nested ``_inbox_lock()`` acquisition, which self-deadlocks
    (see above). No audit event is emitted for a prune.

    Args:
        before: Prune events with ``created_at`` earlier than this.
        keep: Retain only the newest N events; prune the rest.
        archive: When True, append pruned events to the daily archive file
            before dropping them. When False, discard them outright.

    Returns:
        A :class:`PruneResult` describing what happened.

    Raises:
        CwError: If both or neither of *before*/*keep* are given, or if *keep*
            is negative.
    """
    if (before is None) == (keep is None):
        msg = "prune_events: exactly one of 'before' or 'keep' must be given."
        raise CwError(msg)
    if keep is not None and keep < 0:
        msg = "prune_events: 'keep' must be non-negative."
        raise CwError(msg)

    with _inbox_lock():
        return _prune_events_locked(before=before, keep=keep, archive=archive)


def _prune_events_locked(
    *, before: datetime | None, keep: int | None, archive: bool
) -> PruneResult:
    """Prune the inbox. Caller MUST already hold ``_inbox_lock()``.

    ``_inbox_lock`` is not reentrant (see :func:`prune_events`); this helper
    exists so :func:`record_event`'s auto-prune trigger can reuse the prune
    logic from inside its own already-held lock. Does not validate
    ``before``/``keep`` exclusivity -- callers are responsible (see
    :func:`prune_events` for the validated public entry point).
    """
    inbox = inbox_path()
    raw_text = inbox.read_text() if inbox.exists() else ""
    if not raw_text:
        return PruneResult(
            archived_count=0, deleted_count=0, archive_path=None, kept_count=0
        )

    events = _parse_lines(raw_text.splitlines())
    if before is not None:
        kept_events, pruned_events = _partition_events_by_before(events, before)
    elif keep is not None:
        kept_events, pruned_events = _partition_events_by_keep(events, keep)
    else:  # pragma: no cover - unreachable, guarded by validation above
        msg = "prune_events: exactly one of 'before' or 'keep' must be given."
        raise CwError(msg)

    archived_count = 0
    deleted_count = 0
    archive_path: str | None = None
    if pruned_events:
        if archive:
            path = _archive_path_for_today()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Why: archive-append happens before the inbox rewrite below, not
            # after. This ordering is deliberate (#1980 review): a crash
            # between the two steps then loses nothing -- the archive already
            # holds the pruned batch and the inbox is untouched, so a retry
            # at worst double-archives on the next successful prune. The
            # reverse order was the original shape here, accepted when this
            # path was only reachable via an operator-invoked, non-hot-path
            # `cw event prune` -- a human present to notice a crash and
            # re-run. record_event's auto-prune trigger removed both
            # properties: this sequence is now reachable automatically,
            # unattended, from record_event's 114+ call sites, for as long as
            # the process stays above the retention threshold. Duplicate
            # records in a best-effort append-only audit log are benign;
            # permanently vanished ones are not -- that asymmetry is why
            # archive-first is correct now that a human isn't there to catch
            # the old ordering's failure mode.
            with path.open("a") as f:
                for ev in pruned_events:
                    f.write(ev.model_dump_json() + "\n")
            archived_count = len(pruned_events)
            archive_path = str(path)
        else:
            deleted_count = len(pruned_events)

    new_text = "".join(e.model_dump_json() + "\n" for e in kept_events)
    atomic_write_text(inbox, new_text)

    return PruneResult(
        archived_count=archived_count,
        deleted_count=deleted_count,
        archive_path=archive_path,
        kept_count=len(kept_events),
    )


def load_cursor(consumer: str) -> str | None:
    """Return the last-consumed event ID for *consumer*, or None if no cursor."""
    path = _cursor_path(consumer)
    if not path.exists():
        return None
    data: dict[str, str] = json.loads(path.read_text())
    return data.get("cursor")


def advance_cursor(consumer: str, event_id: str) -> None:
    """Persist a consumer's cursor to the given event ID.

    Args:
        consumer: Consumer name (used as filename stem).
        event_id: The event ID to advance the cursor to.
    """
    path = _cursor_path(consumer)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {
        "cursor": event_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_text(path, json.dumps(data))


def init_cursor_at_end(consumer: str) -> bool:
    """Initialize a fresh consumer cursor to the current end of the inbox.

    If the consumer already has a persisted cursor, this function is a no-op
    and returns False.  If the inbox is empty, no cursor is written and False
    is returned.  Otherwise, advances the cursor to the last event in the inbox
    and returns True.

    Use this before the first ``read_events(consumer=...)`` call when you want
    "new events only" semantics rather than replaying history.
    """
    if _cursor_path(consumer).exists():
        return False
    inbox = inbox_path()
    with _inbox_lock():
        raw_text = inbox.read_text() if inbox.exists() else ""
    # Why: _inbox_lock is released before advance_cursor runs, so events appended
    # in that window will appear to be "before" the cursor and be skipped on the
    # first read.  The race is accepted: the consumer sees at-most-once semantics
    # on startup; any missed events are benign (follow-mode replays on size change).
    if not raw_text:
        return False
    parsed = _parse_lines(raw_text.splitlines())
    if not parsed:
        return False
    advance_cursor(consumer, parsed[-1].id)
    return True


def _event_matches(
    event: OrchestratorEvent,
    *,
    since_ts: datetime | None,
    event_types: list[OrchestratorEventType] | None,
    client_names: frozenset[str] | None = None,
    lane_names: frozenset[str] | None = None,
) -> bool:
    """Return True if *event* passes the timestamp, type, client, and lane filters."""
    if since_ts is not None and event.created_at < since_ts:
        return False
    if event_types is not None and event.type not in event_types:
        return False
    if client_names is not None and event.payload.get("client") not in client_names:
        return False
    return not (lane_names is not None and event.payload.get("lane") not in lane_names)


def _parse_lines(lines: list[str]) -> list[OrchestratorEvent]:
    """Parse JSONL lines into a list of events.

    Tolerates a malformed trailing line (torn write): if the last non-empty
    line fails JSON parsing, it is skipped with a warning.  Interior corrupt
    lines re-raise so callers see real corruption.

    Also tolerates a well-formed line whose ``type`` is not a member of the
    in-memory ``OrchestratorEventType`` enum (issue #1210): a newer producer
    may emit an event type an older, long-running consumer's process does not
    yet know about.  Such lines are skipped and summarized in a single
    warning per call.  Any other validation failure -- including an unknown
    type combined with a second, unrelated bad field -- still raises, so
    genuine interior corruption stays loud (issue #393's contract).
    """
    # Precompute last non-empty index so trailing blank lines don't cause the
    # torn-write check to misfire on an interior corrupt line.
    last_nonempty_idx = max(
        (j for j, line in enumerate(lines) if line.strip()), default=-1
    )
    results: list[OrchestratorEvent] = []
    unknown_types: set[str] = set()
    unknown_type_count = 0
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if i == last_nonempty_idx:
                # Tolerate malformed trailing line only (torn write)
                logger.warning("skipping malformed trailing line in inbox: %s", exc)
                continue
            raise
        try:
            results.append(OrchestratorEvent.model_validate(raw))
        except ValidationError as exc:
            errors = exc.errors()
            if all(err["type"] == "enum" and err["loc"] == ("type",) for err in errors):
                unknown_types.add(raw.get("type", "<missing>"))
                unknown_type_count += 1
                continue
            raise
    if unknown_types:
        logger.warning(
            "skipping %d event(s) with unknown type: %s",
            unknown_type_count,
            ", ".join(sorted(unknown_types)),
        )
    return results


def _apply_cursor(
    parsed: list[OrchestratorEvent], cursor: str | None
) -> tuple[list[OrchestratorEvent], bool]:
    """Return the events strictly after *cursor*, plus whether it was found.

    Positioning is applied to the *unfiltered* stream so a cursor event that
    would itself fail the caller's filters still anchors the position.

    ``None`` and "not present" are different answers, not the same one: no
    cursor returns the full list with ``True`` (start from the beginning, as
    asked), while a cursor that is not in the list returns the full list with
    ``False``, leaving the replay decision — and its warning — to the caller.
    """
    if cursor is None:
        return parsed, True
    for i, event in enumerate(parsed):
        if event.id == cursor:
            return parsed[i + 1 :], True
    return parsed, False


class _InboxRead(NamedTuple):
    """One consistent observation of the inbox: its bytes and its identity.

    ``size``/``ino`` come from an ``fstat`` on the *same* descriptor the lines
    were read from, so they cannot describe a different file than the bytes do.
    Taking them from a separate ``stat`` call reopens the replace-between-calls
    window the inode check exists to close.
    """

    lines: list[str]
    offset: int
    size: int
    ino: int


def _read_lines_after_offset(offset: int) -> _InboxRead:
    """Return whole lines appended past *offset*, plus the new consumed offset.

    This is the primitive that makes following cheap (#1979): the follow loops
    previously answered "what is new?" by re-reading and re-parsing the entire
    inbox on every poll, because ``read_events`` applies its cursor *after*
    parsing. Cost is now proportional to the bytes appended, not to history.

    Only whole lines are consumed: the offset advances to the last newline in
    the chunk, so a torn append (a writer caught mid-``write``) stays
    unconsumed and is picked up intact on the next poll rather than being
    parsed as corruption.

    No lock is taken. ``record_event`` appends under ``_inbox_lock``, so a
    reader either sees a whole appended line or stops short of it. A
    ``prune_events`` rewrite lands on a new inode (``atomic_write_text`` is
    temp file + ``Path.replace``), which :func:`_poll_follow_state` detects
    and handles by re-resolving position — this function never seeks into a
    file it has not been told is still the same one.
    """
    try:
        with inbox_path().open("rb") as handle:
            stat = os.fstat(handle.fileno())
            handle.seek(offset)
            chunk = handle.read()
    except FileNotFoundError:
        # Follower started before the first record_event; poll until it exists.
        return _InboxRead([], offset, 0, 0)
    except OSError:
        # Anything else (permissions, disk fault) is not a "not yet" condition.
        # Swallowing it silently would leave a daemon looking healthy while it
        # never sees another event again, so say so and let the caller retry.
        logger.exception("failed reading inbox at offset %d", offset)
        return _InboxRead([], offset, 0, 0)

    newline = chunk.rfind(b"\n")
    if newline == -1:
        return _InboxRead([], offset, stat.st_size, stat.st_ino)

    complete = chunk[: newline + 1]
    # Cutting at b"\n" is always a valid UTF-8 boundary: 0x0A never appears as
    # a continuation byte, so no multi-byte character can be split here.
    lines = complete.decode("utf-8").splitlines()
    return _InboxRead(lines, offset + len(complete), stat.st_size, stat.st_ino)


def _resolve_follow_start(
    since_cursor: str | None,
) -> tuple[list[OrchestratorEvent], _InboxRead]:
    """Resolve *since_cursor* to a byte offset with one full read.

    Followers pay this whole-inbox cost once at startup; every later poll goes
    through :func:`_read_lines_after_offset`. Returns the events after the
    cursor — unfiltered, since callers apply their own filters — and the read
    that produced them, so a caller can seed follow state from a single
    consistent observation rather than a separate ``stat``.
    """
    read = _read_lines_after_offset(0)
    parsed = _parse_lines(read.lines)
    after_cursor, found = _apply_cursor(parsed, since_cursor)
    # An empty inbox has lost nothing, so it warrants no warning — read_events
    # short-circuits on `if not raw_text` before cursor resolution for the same
    # reason. Only a populated inbox that lacks the cursor is a real miss.
    if not found and parsed:
        logger.warning(
            "cursor %s not found in inbox; replaying from start", since_cursor
        )
    return after_cursor, read


def read_events(
    consumer: str | None = None,
    *,
    since_cursor: str | None = None,
    since_ts: datetime | None = None,
    event_types: list[OrchestratorEventType] | None = None,
    client_names: frozenset[str] | None = None,
    lane_names: frozenset[str] | None = None,
    limit: int | None = None,
) -> list[OrchestratorEvent]:
    """Read events from the inbox, optionally filtered.

    When *consumer* is provided and no explicit *since_cursor* is given,
    the consumer's persisted cursor is used automatically.

    When *since_cursor* is provided (or derived from the consumer cursor),
    only events *after* the referenced event ID are returned.

    At-least-once delivery contract: if a consumer's cursor is not found in the
    inbox (e.g. due to inbox rotation or compaction), all events are replayed
    from the beginning. Callers using a named consumer cursor must be idempotent.

    Args:
        consumer: Consumer name; used to load a persisted cursor when
            *since_cursor* is not explicitly set.
        since_cursor: Skip events up to and including this event ID.
        since_ts: Skip events with created_at before this timestamp.
        event_types: If set, only return events of these types.
        client_names: If set, only return events whose payload.client is in this set.
        lane_names: If set, only return events whose payload.lane is in this set.
        limit: If set, return at most the most recent N matching events
            (i.e. the tail of the chronologically-ordered result — not the
            first N). ``limit=0`` returns an empty list.

    Returns:
        List of matching events in ascending (chronological) order.
    """
    # Resolve cursor: explicit arg beats consumer persisted cursor
    cursor = since_cursor
    if cursor is None and consumer is not None:
        cursor = load_cursor(consumer)

    inbox = inbox_path()
    with _inbox_lock():
        raw_text = inbox.read_text() if inbox.exists() else ""

    if not raw_text:
        return []

    lines = raw_text.splitlines()
    parsed = _parse_lines(lines)

    after_cursor, found = _apply_cursor(parsed, cursor)

    # Cursor-not-found fallback: replay from the beginning.
    # Why: callers are idempotent — orchestrate_retire guards on sess.status ==
    # COMPLETED, dispatch consumer skips non-RUNNING tasks, and event tail is
    # display-only — so replaying from the start is safe.
    # `and parsed` matches _resolve_follow_start: an inbox that parsed to
    # nothing (empty, or a single tolerated torn line) has lost no cursor, and
    # "replaying from start" would be a false alarm in a daemon's logs.
    if not found and parsed:
        logger.warning("cursor %s not found in inbox; replaying from start", cursor)

    events = [
        event
        for event in after_cursor
        if _event_matches(
            event,
            since_ts=since_ts,
            event_types=event_types,
            client_names=client_names,
            lane_names=lane_names,
        )
    ]

    if limit is not None:
        # events[-limit:] is correct for limit > 0; limit=0 is a Python trap
        # (list[-0:] == list[:], i.e. the whole list) so it must be special-cased.
        events = events[-limit:] if limit > 0 else []

    return events


_FOLLOW_POLL_INTERVAL: float = 0.05  # 50ms — satisfies ≤100ms acceptance criterion


class _FollowState(NamedTuple):
    """Position of a follower within the inbox.

    Four distinct facts, deliberately not collapsed into fewer (#1979):

    * *offset* — bytes actually consumed. Advances only past whole lines.
    * *seen_size* — file size at the last poll. Drives change detection. This
      must stay separate from *offset*: a torn trailing append leaves bytes on
      disk that are correctly refused, so *offset* stops advancing while the
      size keeps reporting them. Comparing size against *offset* would then
      report "changed" on every poll forever and the follower would never
      reach its idle path.
    * *ino* — inode being read. ``prune_events``/``_prune_events_locked``
      rewrite the inbox via ``atomic_write_text`` (temp file +
      ``Path.replace``), so a rewrite is a *new* inode -- whether triggered
      by the operator-invoked ``cw event prune`` CLI or by ``record_event``'s
      in-band auto-prune trigger (#1980); this class has no notion of who
      triggered the rewrite. Size arithmetic alone cannot see a
      replace-then-regrow that lands above the old offset, and seeking into
      an unrelated file lands mid-line.
    * *cursor* — id of the last event delivered, used to re-establish
      position when the offset stops meaning anything.
    """

    offset: int
    seen_size: int
    ino: int
    cursor: str | None


def _stat_inbox() -> tuple[int, int] | None:
    """Return ``(size, inode)``, ``(0, 0)`` when absent, or ``None`` on failure.

    Three outcomes, not two. An absent inbox is ``(0, 0)`` — a follower started
    before the first ``record_event`` polls until the file appears. But a stat
    that *fails* (permissions, a network filesystem hiccup) is ``None``, not
    ``(0, 0)``: reporting it as "size 0, inode 0" is indistinguishable from a
    replaced file, which would fire a false "inbox replaced" warning, force a
    full re-resolve, and store a zeroed identity that makes the *next* poll
    re-resolve a second time — silently, because the zeroed inode also defeats
    the first-poll warning guard. Mirrors the distinction
    :func:`_read_lines_after_offset` already draws.
    """
    try:
        stat = inbox_path().stat()
    except FileNotFoundError:
        return 0, 0
    except OSError:
        logger.exception("failed to stat inbox")
        return None
    return stat.st_size, stat.st_ino


def _poll_follow_state(
    state: _FollowState,
) -> tuple[list[OrchestratorEvent], _FollowState]:
    """Return events appended since *state*, plus the advanced state.

    Steady state reads only the bytes past ``state.offset`` — the #1979 win.

    When the inbox is replaced or truncated the byte offset no longer refers
    to anything, and this falls back to the pre-#1979 behaviour: a full read
    positioned by the last delivered event id, replaying from the start when
    that id is gone. That fallback is what preserves the at-least-once
    contract, and it is not merely defensive. ``prune_events`` keeps a
    *suffix*, so if a follower's cursor was pruned away then every event at or
    before it was pruned too — which makes the surviving events exactly the
    ones that follower has never seen. Skipping to the new EOF instead would
    silently drop them.

    A rewrite detected here may now originate from ``record_event``'s in-band
    auto-prune trigger (#1980), not only from the operator-invoked
    ``cw event prune`` CLI — the rewrite can therefore land at any moment,
    including mid-poll or mid-write from a concurrent ``record_event`` call.
    The detection above is keyed off ``(ino, size)`` observed via
    ``_stat_inbox()``, not off who triggered the rewrite, so it composes
    unchanged with either origin.
    """
    stat = _stat_inbox()
    if stat is None:
        # Transient failure. Change nothing and retry on the next poll —
        # anything else would misreport a failed stat as a replaced file.
        return [], state
    size, ino = stat

    if ino != state.ino or size < state.offset:
        if state.ino:  # not the first poll — a real replacement, worth saying
            logger.warning(
                "inbox replaced or truncated (inode %d -> %d, size %d -> %d); "
                "re-resolving follow position from cursor %s",
                state.ino,
                ino,
                state.seen_size,
                size,
                state.cursor,
            )
        events, read = _resolve_follow_start(state.cursor)
        cursor = events[-1].id if events else state.cursor
        return events, _FollowState(read.offset, read.size, read.ino, cursor)

    if size == state.seen_size:
        return [], state._replace(seen_size=size)

    read = _read_lines_after_offset(state.offset)
    events = _parse_lines(read.lines)
    cursor = events[-1].id if events else state.cursor
    return events, _FollowState(read.offset, read.size, read.ino, cursor)


def _event_matches_wait(
    event: OrchestratorEvent,
    *,
    correlation_id: str | None,
    session_id: str | None,
    client: str | None,
) -> bool:
    """Return True if event passes correlation_id, session_id, and client filters."""
    if correlation_id is not None and event.correlation_id != correlation_id:
        return False
    if session_id is not None and event.payload.get("session_id") != session_id:
        return False
    return client is None or event.payload.get("client") == client


def wait_for_event(
    *,
    event_types: list[OrchestratorEventType] | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    client: str | None = None,
    timeout: float = 3600.0,
    follow: bool = False,
    poll_interval: float = _FOLLOW_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield matching events from the inbox, blocking until they arrive.

    Reads from the beginning of the inbox so events recorded before the
    call started are also matched.  Polls every *poll_interval* seconds via
    :func:`_poll_follow_state`, which reads only bytes appended since the
    last poll and skips the read entirely when the size is unchanged.

    In default mode (follow=False) exits after the first match.  With
    follow=True streams all matches until timeout.

    Args:
        event_types: Filter by event type(s).
        correlation_id: Filter by event.correlation_id.
        session_id: Filter by payload["session_id"].
        client: Filter by payload["client"].
        timeout: Seconds before raising TimeoutError.
        follow: If True, keep streaming after first match.
        poll_interval: Seconds between inbox polls.

    Raises:
        TimeoutError: If no match arrives within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    matched = False

    # No cursor: wait_for_event matches against pre-existing history too.
    new_events, read = _resolve_follow_start(None)
    state = _FollowState(
        read.offset, read.size, read.ino, new_events[-1].id if new_events else None
    )

    while True:
        for ev in new_events:
            if not _event_matches(
                ev,
                since_ts=None,
                event_types=event_types,
                client_names=None,
                lane_names=None,
            ):
                continue
            if _event_matches_wait(
                ev,
                correlation_id=correlation_id,
                session_id=session_id,
                client=client,
            ):
                matched = True
                yield ev
                if not follow:
                    return

        if time.monotonic() >= deadline:
            if not matched:
                msg = f"No matching event after {timeout:.0f}s"
                raise TimeoutError(msg)
            return

        time.sleep(poll_interval)

        new_events, state = _poll_follow_state(state)


def tail_events_follow(
    *,
    since_cursor: str | None,
    since_ts: datetime | None,
    event_types: list[OrchestratorEventType] | None,
    client_names: frozenset[str] | None = None,
    lane_names: frozenset[str] | None = None,
    poll_interval: float = _FOLLOW_POLL_INTERVAL,
) -> Generator[OrchestratorEvent]:
    """Yield new events as they arrive, polling the inbox for changes.

    Resolves *since_cursor* to a byte offset with a single full read at
    startup, then reads only the bytes appended past that offset on each poll
    (#1979).  Change detection is a ``stat`` against the size seen at the
    previous poll, so an unchanged inbox costs one syscall.

    When ``prune_events`` rewrites the inbox, :func:`_poll_follow_state`
    re-resolves position from the last delivered event id and replays the
    surviving events the follower has not seen — preserving the at-least-once
    contract rather than skipping to the new EOF.  Holds no lock at any
    point.  Does not advance any consumer cursor — that remains one-shot
    only.

    Exits when the caller sends a ``GeneratorExit`` (or raises
    ``KeyboardInterrupt`` / ``BrokenPipeError`` in the iterating loop).
    """
    new_events, read = _resolve_follow_start(since_cursor)
    state = _FollowState(
        read.offset,
        read.size,
        read.ino,
        new_events[-1].id if new_events else since_cursor,
    )

    while True:
        for event in new_events:
            if _event_matches(
                event,
                since_ts=since_ts,
                event_types=event_types,
                client_names=client_names,
                lane_names=lane_names,
            ):
                yield event

        time.sleep(poll_interval)

        new_events, state = _poll_follow_state(state)
