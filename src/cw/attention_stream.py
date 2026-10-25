"""The persistent attention stream behind ``cw orchestrate watch`` (#2693).

One long-lived process per (client, scope): it follows the event inbox in
process (:class:`cw.events_follow.InboxFollower`), prints one line per event
that needs the orchestrator (:mod:`cw.attention_watch` decides which and how),
and every 30 s scans the in-scope PENDING rows for ones that will not clear on
their own (:mod:`cw.stuck_pending`). Silence means healthy.

**Cursor.** The position is the consumer cursor record
``events_dir()/cursors/<scope consumer>.json``, whose ``extra`` holds the
dedupe latch, the owner pid and the scope. It is saved after every poll batch
that read anything -- *after* the batch's lines are printed and flushed, so an
event written to a dead stdout is replayed on the next arm (at-least-once).
A batch that renders nothing still saves, so filtered events are not re-read.

**Start.** ``--since`` starts after that event; else a saved non-null cursor
resumes; else the start is "now", the newest inbox event. A first arm over an
empty inbox saves ``cursor: null`` with ``armed_empty: true``, and the next
arm resolves that to "now" again rather than replaying the inbox from the
start (events appended while no watcher ran are skipped, as on a first arm).

**Owner pid.** A record naming another live pid refuses the start unless
``--force``. ``os.kill(pid, 0)`` cannot tell a recycled pid from the old
watcher, so a stale record after a restart needs ``--force``; there is no
ownership re-check before a save, so two watchers on one scope can regress
each other's cursor (duplicates, never loss).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

from cw.attention_watch import (
    WATCHED_TYPES,
    Latch,
    RowIndex,
    error_reason,
    exit_line,
    first_next_command,
    in_scope,
    refused_reason,
    render_event,
    render_scope_line,
    render_stuck,
    signal_reason,
    write_failed_reason,
)
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.events_follow import (
    InboxFollower,
    last_event_id,
    load_cursor_record,
    save_cursor_record,
)
from cw.models import QueueItemStatus
from cw.reconcile._shared import resolve_session_for_task
from cw.stuck_pending import SECONDS_PER_MINUTE, PendingTracker, scan_pending
from cw.ticket_view import ViewSources, next_command_facts

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    from cw.attention_watch import WatchScope
    from cw.models import CwState, OrchestratorEvent, TicketTask

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
STUCK_SCAN_SECONDS = 30.0
EXIT_SIGTERM = 0
EXIT_ERROR = 1
EXIT_SIGINT = 130

START_RESUME = "resume"
START_NOW = "now"
START_SINCE = "since"

_SIGNAL_EXIT: dict[str, int] = {"SIGTERM": EXIT_SIGTERM, "SIGINT": EXIT_SIGINT}


class _Stopped(BaseException):
    """A terminating signal; a ``BaseException`` so no broad handler eats it."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _StdoutFailedError(Exception):
    """Writing or flushing the stream's stdout raised *cause*."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _raise_stopped(signum: int, _frame: FrameType | None) -> None:
    raise _Stopped(signal.Signals(signum).name)


@contextlib.contextmanager
def _signals_stop() -> Iterator[None]:
    """Turn SIGTERM/SIGINT into :class:`_Stopped` (main thread only)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {
        sig: signal.signal(sig, _raise_stopped)
        for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _pid_alive(pid: int) -> bool:
    """Best effort: True when a process with *pid* exists (or is not ours)."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _emit(out: TextIO, lines: list[str]) -> None:
    """Write *lines* and flush; any ``OSError`` becomes :class:`_StdoutFailedError`."""
    try:
        out.writelines(f"{line}\n" for line in lines)
        out.flush()
    except OSError as exc:
        raise _StdoutFailedError(exc) from exc


def _final_line(out: TextIO, err: TextIO, line: str) -> None:
    """Print the exit line on stdout, else on stderr when stdout is dead."""
    try:
        _emit(out, [line])
    except _StdoutFailedError:
        err.write(f"{line}\n")
        err.flush()


class AttentionStream:
    """One watcher: a scope, its persisted cursor and latch, and the stuck scan."""

    def __init__(
        self,
        scope: WatchScope,
        *,
        stuck_after_minutes: int,
        since: str | None,
        force: bool,
        pid: int | None = None,
    ) -> None:
        self._scope = scope
        self._since = since
        self._force = force
        self._pid = os.getpid() if pid is None else pid
        self._tracker = PendingTracker(stuck_after_minutes * SECONDS_PER_MINUTE)
        self._latch = Latch()
        self._follower = InboxFollower(None)
        self._unsaved = False

    # --- startup ------------------------------------------------------------

    def _resolve_start(
        self, record: dict[str, object] | None
    ) -> tuple[str, str | None]:
        """``(start mode, cursor)``: since, else a saved cursor, else now."""
        if self._since is not None:
            return START_SINCE, self._since
        saved = record.get("cursor") if record is not None else None
        if isinstance(saved, str):
            return START_RESUME, saved
        return START_NOW, last_event_id()

    def _refusing_owner(self, extra: dict[str, object]) -> int | None:
        """The live foreign pid that owns this cursor, unless ``--force``."""
        owner = extra.get("owner_pid")
        if self._force or not isinstance(owner, int) or owner == self._pid:
            return None
        return owner if _pid_alive(owner) else None

    def _save(self, cursor: str | None) -> None:
        save_cursor_record(
            self._scope.consumer,
            cursor,
            {
                "latch": self._latch.to_dict(),
                "owner_pid": self._pid,
                "scope": self._scope.to_dict(),
                "armed_empty": cursor is None,
            },
        )

    # --- one poll -------------------------------------------------------------

    def step(self) -> list[str]:
        """Poll the inbox once; return the lines to print for what arrived.

        Clear events are applied to the latch before scope filtering (D6) and
        never rendered. Persisting is the caller's job, after printing.
        """
        events = self._follower.poll()
        if not events:
            return []
        self._unsaved = True
        watched = [
            e for e in events if not self._latch.clear(e) and e.type in WATCHED_TYPES
        ]
        if not watched:
            return []
        rows = RowIndex(load_dev_queue(), self._scope.client)
        state: list[CwState] = []
        sources = ViewSources()
        return [
            render_event(event, self._command(event, rows, state, sources))
            for event in watched
            if in_scope(event, self._scope, rows) and self._latch.admit(event)
        ]

    @staticmethod
    def _command(
        event: OrchestratorEvent,
        rows: RowIndex,
        state: list[CwState],
        sources: ViewSources,
    ) -> str | None:
        """The event row's first next command; *state* caches one state load.

        *sources* shares the page's config reads; the human opt-in and the
        pipeline position are resolved as ``show`` resolves them (#2760 R7).
        """
        ticket = event.payload.get("ticket_id")
        row = rows.row(ticket) if isinstance(ticket, str) and ticket else None
        if row is None:
            return None
        if not state:
            state.append(load_state())
        probe = scan_pending([row], now=datetime.now(UTC))
        block = probe[0].block if probe else None
        facts = next_command_facts(row, sources)
        return first_next_command(
            row,
            resolve_session_for_task(row, state[0]),
            skip_reason=block.code if block is not None else None,
            lane_resume_command=block.command if block is not None else None,
            operator_opt_in=facts.operator_opt_in,
            pipeline_position=facts.position,
        )

    # --- the stuck scan -------------------------------------------------------

    def _row_in_scope(self, row: TicketTask) -> bool:
        scope = self._scope
        return (
            scope.everything
            or row.ticket_id in scope.tickets
            or row.lane in scope.lanes
        )

    def scan_stuck(self, at: float, now: datetime) -> list[str]:
        """STUCK lines for in-scope PENDING rows past the threshold (once each).

        *at* is monotonic seconds (the age clock); *now* dates the lines. No
        ticket view is built while no in-scope row is PENDING.
        """
        rows = {
            row.ticket_id: row
            for row in load_dev_queue().tasks
            if row.client == self._scope.client
            and row.status == QueueItemStatus.PENDING
            and self._row_in_scope(row)
        }
        if not rows:
            self._tracker.retain(set())
            return []
        lines: list[str] = []
        successful: set[str] = set()
        for probe in scan_pending(rows.values(), now=now):
            successful.add(probe.view.ticket_id)
            age = self._tracker.observe(probe.view.ticket_id, probe.block, at)
            if age is not None and probe.block is not None:
                lines.append(
                    render_stuck(
                        now,
                        ticket_id=probe.view.ticket_id,
                        lane=probe.view.lane,
                        block=probe.block,
                        age_seconds=age,
                    )
                )
        self._tracker.retain(successful)
        return lines

    # --- the loop ---------------------------------------------------------------

    def run(
        self,
        out: TextIO,
        *,
        err: TextIO,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        now_fn: Callable[[], datetime],
    ) -> int:
        """Stream until a signal or a fatal error; return the exit code.

        0 on SIGTERM, 130 on SIGINT, 1 on a refused start, a stdout write
        failure or any other error. The last line printed is always
        ``WATCH | exited: <reason>`` (on stderr when stdout is the failure).
        """
        with _signals_stop():
            try:
                return self._serve(out, sleep=sleep, clock=clock, now_fn=now_fn)
            except _StdoutFailedError as failed:
                err.write(f"{exit_line(write_failed_reason(failed.cause))}\n")
                err.flush()
                return EXIT_ERROR
            except _Stopped as stopped:
                _final_line(out, err, exit_line(signal_reason(stopped.name)))
                return _SIGNAL_EXIT.get(stopped.name, EXIT_ERROR)
            except KeyboardInterrupt:
                _final_line(out, err, exit_line(signal_reason("SIGINT")))
                return EXIT_SIGINT
            except Exception as exc:
                logger.exception("cw orchestrate watch failed")
                _final_line(out, err, exit_line(error_reason(exc)))
                return EXIT_ERROR

    def _serve(
        self,
        out: TextIO,
        *,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        now_fn: Callable[[], datetime],
    ) -> int:
        record = load_cursor_record(self._scope.consumer)
        raw_extra = record.get("extra") if record is not None else None
        extra: dict[str, object] = raw_extra if isinstance(raw_extra, dict) else {}
        start, cursor = self._resolve_start(record)
        scope_line = render_scope_line(self._scope, start)
        owner = self._refusing_owner(extra)
        if owner is not None:
            _emit(
                out,
                [scope_line, exit_line(refused_reason(owner, self._scope.consumer))],
            )
            return EXIT_ERROR
        self._latch = Latch.from_dict(extra.get("latch"))
        self._follower = InboxFollower(cursor)
        self._save(cursor)
        _emit(out, [scope_line])
        next_scan = clock()
        while True:
            lines = self.step()
            if clock() >= next_scan:
                lines.extend(self.scan_stuck(clock(), now_fn()))
                next_scan = clock() + STUCK_SCAN_SECONDS
            if lines:
                _emit(out, lines)
            if self._unsaved:
                self._save(self._follower.cursor)
                self._unsaved = False
            sleep(POLL_SECONDS)
