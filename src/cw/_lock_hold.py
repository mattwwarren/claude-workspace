"""Lock-hold recorder for the sessions and dev-queue locks (#2637).

``cw.config._guarded_state_lock`` is the one seam every ``sessions_lock()`` and
``dev_queue_lock()`` acquisition goes through. It times each acquisition and
hands the numbers here:

- **Hold**: from ``acquire(fd)`` returning to just before ``LOCK_UN``.
  **Wait**: from just before ``acquire(fd)`` to it returning. A bounded
  acquisition that times out was never held and leaves no record.
- **State I/O**: :func:`timed_state_io` wraps ``load_state``/``save_state``
  and, while this thread holds a SESSIONS-rank lock, adds its elapsed time to a
  thread-local accumulator. Each sessions record carries that sum as
  ``state_io_s`` (clamped to ``held_s``) and the ``sessions.json`` size.
- **Caller**: ``module:qualname`` of the first frame outside the lock
  plumbing (``contextlib``, ``cw.config``, ``cw.dev_queue.storage``, this
  module), so the ~190 acquisition sites need no ``caller=`` argument.

Cost model. Per guarded acquisition the seam adds three ``time.monotonic()``
reads; two of them bracket the hold. Inside a sessions hold each
``load_state``/``save_state`` call adds two more ``monotonic()`` reads and one
scan of the thread's held-lock stack. :func:`note_hold` runs after
``LOCK_UN``: for a hold below :data:`RECORD_MIN_S` it is a call and two
comparisons; a qualifying hold adds a frame walk, ``datetime.now`` and one
model build. All in memory. The I/O (one ``stat`` of ``sessions.json``, the
debug log lines, the sidecar append) runs in :func:`flush_pending`, and only
once the thread holds no guarded lock, so the sessions lock is never held
while the sidecar is written. No lock is taken and no subprocess is run.

A hold is recorded when ``held_s`` or ``waited_s`` is at least
:data:`RECORD_MIN_S`. Records go to a lock-free dated JSONL sidecar,
``lock_holds.<YYYY-MM-DD>.jsonl`` (UTC) in the state dir, capped per day (see
:func:`select_lines`). Every noted hold also gets one DEBUG line on this
module's logger before the cap applies, so the debug log is the uncapped
channel. Only completed holds are recorded: a hold still in progress, or a
process killed mid-hold, leaves nothing.

The recorder is failure-isolated: :func:`note_hold`, :func:`flush_pending`
and :func:`timed_state_io` swallow any ``Exception`` from their own
bookkeeping (logged at DEBUG), so the recorder can never replace or raise
over the outcome of a lock-protected operation. ``CW_LOCK_HOLD_RECORD=0``,
read once at import, turns recording and the state-I/O timer off.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from pydantic import BaseModel, field_validator

from cw._lock_guard import LockRank, held_locks, is_rank_held
from cw._util import format_compact_age
from cw.exceptions import DEV_QUEUE_LOCK_NAME, SESSIONS_LOCK_NAME

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path
    from types import TracebackType

_logger = logging.getLogger(__name__)

# ``CW_LOCK_HOLD_RECORD=0`` disables recording (read once at import).
LOCK_HOLD_RECORD_ENV = "CW_LOCK_HOLD_RECORD"
_DISABLED_VALUE = "0"
# Locks whose holds are recorded; ``clients`` and ``concurrency_override``
# share the seam but are not recorded. Summaries list them in this order.
_SUMMARY_ORDER = (SESSIONS_LOCK_NAME, DEV_QUEUE_LOCK_NAME)
TRACKED_LOCKS = frozenset(_SUMMARY_ORDER)
# A hold is recorded when it was held, or waited for, at least this long.
RECORD_MIN_S = 0.25
# Doctor warns at this hold or wait; past the daily cap only such records
# are still appended.
WARN_HELD_S = 10.0
# The window doctor and ``cw dev-queue status`` report on.
RECENT_WINDOW = timedelta(hours=1)
# Aggregate state-I/O share at or above which pruning is the suggested fix.
IO_DOMINATES_FRACTION = 0.5
# Per-UTC-day sidecar cap, and the hard ceiling as a multiple of it.
MAX_FILE_BYTES = 1_048_576
CEILING_FACTOR = 2
# Each append is whole lines in single ``write`` calls of at most this size.
WRITE_CHUNK_BYTES = 4096
CALLER_MAX_CHARS = 200
_PENDING_CAP = 256
_FRAME_DEPTH_CAP = 16
_FLOAT_DIGITS = 3
_UNKNOWN_CALLER = "unknown"
_INFRA_MODULES = frozenset(
    {"contextlib", "cw.config", "cw.dev_queue.storage", "cw._lock_hold"}
)
_FILE_PREFIX = "lock_holds."
_FILE_SUFFIX = ".jsonl"
_MARKER_KIND: Final = "capped"
_CAP_DETAIL = (
    "daily record cap reached; only holds or waits at or above the warn"
    " threshold are recorded until the hard ceiling"
)
_CEILING_DETAIL = "hard ceiling reached; nothing more is written to this file"

type CapStage = Literal["cap", "ceiling"]


def record_enabled_from_env(environ: Mapping[str, str]) -> bool:
    """Return whether recording is on for *environ* (off only for ``"0"``)."""
    return environ.get(LOCK_HOLD_RECORD_ENV) != _DISABLED_VALUE


RECORD_ENABLED = record_enabled_from_env(os.environ)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "timestamp must carry a timezone"
        raise ValueError(msg)
    return value


class LockHoldRecord(BaseModel):
    """One completed, recorded lock hold. ``at`` is the release time (UTC)."""

    at: datetime
    lock: str
    held_s: float
    waited_s: float
    caller: str
    pid: int
    state_io_s: float | None = None
    state_bytes: int | None = None

    @field_validator("at")
    @classmethod
    def _aware_at(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("held_s", "waited_s", "state_io_s")
    @classmethod
    def _round(cls, value: float | None) -> float | None:
        return None if value is None else round(value, _FLOAT_DIGITS)


class _CapMarker(BaseModel):
    """The line written once when a day's sidecar reaches the cap or ceiling."""

    kind: Literal["capped"]
    stage: CapStage
    at: datetime
    cap_bytes: int
    ceiling_bytes: int
    detail: str

    @field_validator("at")
    @classmethod
    def _aware_at(cls, value: datetime) -> datetime:
        return _require_aware(value)


class _ThreadState(threading.local):
    """Per-thread pending notes and the sessions-hold state-I/O accumulator."""

    def __init__(self) -> None:
        self.pending: list[LockHoldRecord] = []
        self.state_io_s = 0.0


_STATE = _ThreadState()


class _LoggedSuppress(contextlib.suppress):
    """Swallow any ``Exception`` from recorder bookkeeping, logging it at DEBUG.

    ``KeyboardInterrupt`` and ``SystemExit`` still propagate.
    """

    def __init__(self, what: str) -> None:
        super().__init__(Exception)
        self._what = what

    def __exit__(
        self,
        exctype: type[BaseException] | None,
        excinst: BaseException | None,
        exctb: TracebackType | None,
    ) -> bool:
        suppressed = super().__exit__(exctype, excinst, exctb)
        if suppressed and exctype is not None:
            _logger.debug(
                "lock-hold recorder %s failed: %s: %s",
                self._what,
                exctype.__name__,
                excinst,
            )
        return suppressed


@contextlib.contextmanager
def timed_state_io() -> Iterator[None]:
    """Time the wrapped state I/O when this thread holds a SESSIONS-rank lock.

    Yields exactly once on every path. The wrapped operation's result and
    exception pass through unchanged; a failing probe or accumulate step only
    costs the measurement.
    """
    started: float | None = None
    with _LoggedSuppress("timed_state_io probe"):
        if RECORD_ENABLED and is_rank_held(LockRank.SESSIONS):
            started = time.monotonic()
    if started is None:
        yield
        return
    try:
        yield
    finally:
        with _LoggedSuppress("timed_state_io"):
            _STATE.state_io_s += time.monotonic() - started


def caller_label() -> str:
    """Return ``module:qualname`` of the first frame outside the lock plumbing."""
    frame = inspect.currentframe()
    try:
        for _ in range(_FRAME_DEPTH_CAP):
            if frame is None:
                break
            module = str(frame.f_globals.get("__name__", ""))
            if module not in _INFRA_MODULES:
                label = f"{module}:{frame.f_code.co_qualname}"
                return label[:CALLER_MAX_CHARS]
            frame = frame.f_back
    finally:
        del frame
    return _UNKNOWN_CALLER


def note_hold(lock_name: str, *, held_s: float, waited_s: float) -> None:
    """Note one completed hold of *lock_name* in memory (no I/O)."""
    with _LoggedSuppress("note_hold"):
        _note(lock_name, held_s=held_s, waited_s=waited_s)


def _note(lock_name: str, *, held_s: float, waited_s: float) -> None:
    state_io_s: float | None = None
    if lock_name == SESSIONS_LOCK_NAME:
        state_io_s = min(_STATE.state_io_s, held_s)
        _STATE.state_io_s = 0.0
    if not RECORD_ENABLED or lock_name not in TRACKED_LOCKS:
        return
    if held_s < RECORD_MIN_S and waited_s < RECORD_MIN_S:
        return
    record = LockHoldRecord(
        at=datetime.now(UTC),
        lock=lock_name,
        held_s=held_s,
        waited_s=waited_s,
        caller=caller_label(),
        pid=os.getpid(),
        state_io_s=state_io_s,
    )
    pending = _STATE.pending
    pending.append(record)
    if len(pending) > _PENDING_CAP:
        del pending[: len(pending) - _PENDING_CAP]


def flush_pending(sink_dir: Callable[[], Path], state_file: Callable[[], Path]) -> None:
    """Log and append this thread's pending notes once it holds no guarded lock.

    *sink_dir* and *state_file* are path accessors, resolved inside the failure
    guard. The batch is taken before any I/O, so a failed write never retries.
    """
    with _LoggedSuppress("flush_pending"):
        _flush(sink_dir, state_file)


def _flush(sink_dir: Callable[[], Path], state_file: Callable[[], Path]) -> None:
    if not RECORD_ENABLED or not _STATE.pending or held_locks():
        return
    batch = _STATE.pending
    _STATE.pending = []
    if any(record.lock == SESSIONS_LOCK_NAME for record in batch):
        state_bytes = _stat_size(state_file)
        batch = [
            record.model_copy(update={"state_bytes": state_bytes})
            if record.lock == SESSIONS_LOCK_NAME
            else record
            for record in batch
        ]
    for record in batch:
        _logger.debug(
            "lock %s held %.3fs (waited %.3fs) by %s pid=%d state_io_s=%s"
            " state_bytes=%s",
            record.lock,
            record.held_s,
            record.waited_s,
            record.caller,
            record.pid,
            record.state_io_s,
            record.state_bytes,
        )
    write_batch(sink_dir(), batch, datetime.now(UTC))


def _stat_size(state_file: Callable[[], Path]) -> int | None:
    try:
        return state_file().stat().st_size
    except OSError:
        return None


def day_path(sink_dir: Path, day: date) -> Path:
    """Return the sidecar path for UTC *day* under *sink_dir*."""
    return sink_dir / f"{_FILE_PREFIX}{day.isoformat()}{_FILE_SUFFIX}"


def write_batch(
    sink_dir: Path, records: Sequence[LockHoldRecord], now: datetime
) -> None:
    """Append *records* to the sidecar for *now*'s UTC day under the cap policy.

    Lines go out in single ``O_APPEND`` writes of whole lines, each at most
    :data:`WRITE_CHUNK_BYTES`, so concurrent writers interleave by chunk and
    the reader skips any torn line.
    """
    today = now.astimezone(UTC).date()
    path = day_path(sink_dir, today)
    sink_dir.mkdir(parents=True, exist_ok=True)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    lines = select_lines(records, size, now)
    if not lines:
        return
    with path.open("ab", buffering=0) as handle:
        for chunk in chunk_lines(lines):
            handle.write(chunk)


def _is_warn_level(record: LockHoldRecord) -> bool:
    return record.held_s >= WARN_HELD_S or record.waited_s >= WARN_HELD_S


def _marker_line(stage: CapStage, now: datetime) -> str:
    marker = _CapMarker(
        kind=_MARKER_KIND,
        stage=stage,
        at=now,
        cap_bytes=MAX_FILE_BYTES,
        ceiling_bytes=MAX_FILE_BYTES * CEILING_FACTOR,
        detail=_CAP_DETAIL if stage == "cap" else _CEILING_DETAIL,
    )
    return marker.model_dump_json() + "\n"


def select_lines(
    records: Sequence[LockHoldRecord], size: int, now: datetime
) -> list[str]:
    """Choose the lines to append to a sidecar that is *size* bytes long.

    Below :data:`MAX_FILE_BYTES` every record is accepted. From the cap up to
    the ceiling (``MAX_FILE_BYTES * CEILING_FACTOR``) only records whose hold
    or wait is at least :data:`WARN_HELD_S` are. The record that carries the
    file across the cap is followed by one ``cap`` marker, and the one that
    carries it across the ceiling by one ``ceiling`` marker; nothing is
    written after that. Markers count toward the size.
    """
    ceiling = MAX_FILE_BYTES * CEILING_FACTOR
    thresholds: tuple[tuple[CapStage, int], ...] = (
        ("cap", MAX_FILE_BYTES),
        ("ceiling", ceiling),
    )
    lines: list[str] = []
    for record in records:
        if size >= ceiling or (size >= MAX_FILE_BYTES and not _is_warn_level(record)):
            continue
        before = size
        line = record.model_dump_json() + "\n"
        lines.append(line)
        size += len(line.encode())
        for stage, threshold in thresholds:
            if before < threshold <= size:
                marker = _marker_line(stage, now)
                lines.append(marker)
                size += len(marker.encode())
    return lines


def chunk_lines(lines: Sequence[str], limit: int = WRITE_CHUNK_BYTES) -> list[bytes]:
    """Pack whole *lines* into byte chunks of at most *limit* bytes.

    A single line longer than *limit* goes out alone (lines are never split).
    """
    chunks: list[bytes] = []
    current = b""
    for line in lines:
        encoded = line.encode()
        if current and len(current) + len(encoded) > limit:
            chunks.append(current)
            current = b""
        current += encoded
    if current:
        chunks.append(current)
    return chunks


class HoldWindow(NamedTuple):
    """Recorded holds in a window, and whether a relevant cap marker was seen."""

    records: list[LockHoldRecord]
    capped: bool
    ceiling: bool


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_line(line: str) -> LockHoldRecord | _CapMarker | None:
    try:
        payload = json.loads(line)
        if isinstance(payload, dict) and payload.get("kind") == _MARKER_KIND:
            return _CapMarker.model_validate(payload)
        return LockHoldRecord.model_validate(payload)
    except ValueError:
        return None


def _marker_relevant(marker: _CapMarker, start: datetime) -> bool:
    """A marker applies when its UTC day ends after the window starts."""
    day = marker.at.astimezone(UTC).date()
    day_end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return day_end > start


def read_window(
    sink_dir: Path, *, now: datetime, window: timedelta = RECENT_WINDOW
) -> HoldWindow:
    """Read the records released in the *window* before *now*.

    Reads yesterday's and today's sidecars (enough for a window of up to a
    day). Absent or unreadable files and malformed or torn lines are skipped.
    """
    start = now - window
    today = now.astimezone(UTC).date()
    records: list[LockHoldRecord] = []
    capped = ceiling = False
    for day in (today - timedelta(days=1), today):
        for line in _read_lines(day_path(sink_dir, day)):
            parsed = _parse_line(line)
            if isinstance(parsed, LockHoldRecord):
                if parsed.at >= start:
                    records.append(parsed)
            elif parsed is not None and _marker_relevant(parsed, start):
                capped = True
                ceiling = ceiling or parsed.stage == "ceiling"
    return HoldWindow(records, capped=capped, ceiling=ceiling)


@dataclass(frozen=True, slots=True)
class LockHoldSummary:
    """Per-lock summary of a window's recorded holds.

    A frozen dataclass rather than a ``NamedTuple`` because a ``count`` field
    would shadow ``tuple.count``.

    ``count`` is the number of persisted records, a lower bound once the cap
    trips. ``state_io_fraction`` is ``sum(state_io_s) / sum(held_s)`` over the
    sessions records that carry ``state_io_s`` (``None`` when that sum of
    ``held_s`` is zero); it is not the longest record's ratio. Both it and
    ``state_io_dominates`` are ``None`` for ``dev_queue``.
    """

    lock: str
    count: int
    longest: LockHoldRecord | None
    longest_wait: LockHoldRecord | None
    state_io_fraction: float | None
    state_io_dominates: bool | None


def _state_io_fraction(records: Sequence[LockHoldRecord]) -> float | None:
    timed = [(r.held_s, r.state_io_s) for r in records if r.state_io_s is not None]
    held = sum(held_s for held_s, _ in timed)
    if held <= 0:
        return None
    return sum(io_s for _, io_s in timed) / held


def _summarize_lock(lock: str, records: Sequence[LockHoldRecord]) -> LockHoldSummary:
    fraction: float | None = None
    dominates: bool | None = None
    if lock == SESSIONS_LOCK_NAME:
        fraction = _state_io_fraction(records)
        dominates = fraction is not None and fraction >= IO_DOMINATES_FRACTION
    return LockHoldSummary(
        lock=lock,
        count=len(records),
        longest=max(records, key=lambda r: r.held_s, default=None),
        longest_wait=max(records, key=lambda r: r.waited_s, default=None),
        state_io_fraction=fraction,
        state_io_dominates=dominates,
    )


def summarize(records: Sequence[LockHoldRecord]) -> list[LockHoldSummary]:
    """Summarize *records* per tracked lock that has any (sessions first)."""
    by_lock = {lock: [r for r in records if r.lock == lock] for lock in _SUMMARY_ORDER}
    return [
        _summarize_lock(lock, lock_records)
        for lock, lock_records in by_lock.items()
        if lock_records
    ]


def _format_summary(summary: LockHoldSummary, now: datetime) -> str:
    longest = summary.longest
    if longest is None:
        return f"{summary.lock} n/a"
    detail = f"{longest.caller}, {format_compact_age(now - longest.at)} ago"
    if summary.lock == SESSIONS_LOCK_NAME:
        share = (
            "n/a"
            if summary.state_io_fraction is None
            else f"{summary.state_io_fraction:.0%}"
        )
        detail += f"; aggregate state load/save {share}"
    return f"{summary.lock} {longest.held_s:.1f}s ({detail})"


def format_longest(summaries: Sequence[LockHoldSummary], now: datetime) -> str:
    """Render each lock's longest hold, e.g. ``sessions 12.4s (caller, 14m ago; ...)``.

    Returns ``""`` when there are no summaries. Shared by ``cw doctor`` and
    ``cw dev-queue status``.
    """
    return "; ".join(_format_summary(summary, now) for summary in summaries)
