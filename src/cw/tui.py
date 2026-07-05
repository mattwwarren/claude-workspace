"""Flat live board for the ``cw watch`` command.

Renders the output of :func:`cw.orchestrate.orchestrator_status` as a flat
Rich table of all active work (running sessions + pending tickets), wrapped in
:class:`rich.live.Live` with interactive key handling. The
orchestrator-dashboard render stack moved to ``cw board`` (issue #986); only
the ``cw watch`` flat-board surface lives here now.
"""

from __future__ import annotations

import os
import queue
import select as _sel
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from cw._util import _shorten_worktree
from cw.orchestrate import (
    OrchestratorStatus,
    SessionSummary,
    TicketSummary,
    orchestrator_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType

_MIN_INTERVAL_SECONDS = 1
_MAX_INTERVAL_SECONDS = 60

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _format_elapsed(started_at: datetime, now: datetime) -> str:
    """Return a short ``HhMmSs``-style elapsed time string."""
    delta = max(now - started_at, now - now)  # Guard against clock skew.
    total_seconds = int(delta.total_seconds())
    if total_seconds < _SECONDS_PER_MINUTE:
        return f"{total_seconds}s"
    if total_seconds < _SECONDS_PER_HOUR:
        minutes, seconds = divmod(total_seconds, _SECONDS_PER_MINUTE)
        return f"{minutes}m{seconds:02d}s"
    hours, remainder = divmod(total_seconds, _SECONDS_PER_HOUR)
    minutes = remainder // _SECONDS_PER_MINUTE
    return f"{hours}h{minutes:02d}m"


# ── flat watch board ──────────────────────────────────────────────────────────

_DASH = "—"


class WatchRow(BaseModel):
    """One row in the flat watch table (session OR ticket OR both)."""

    client: str
    ticket_id: str = ""
    queue_status: str = _DASH
    session_status: str = _DASH
    pane_cmd: str = _DASH
    idle_age: str = ""
    last_activity: str = ""
    total_cost_usd: str = _DASH
    session_id: str | None = None
    worktree_path: Path | None = None

    @classmethod
    def from_session(cls, sess: SessionSummary, *, now: datetime) -> WatchRow:
        """Build a row from a running session with no associated ticket."""
        return cls(
            client=sess.client,
            ticket_id="",
            queue_status=_DASH,
            session_status=sess.status,
            idle_age=_format_elapsed(sess.started_at, now),
            last_activity=str(sess.started_at),
            session_id=sess.id,
            worktree_path=sess.worktree_path,
        )

    @classmethod
    def from_ticket(cls, ticket: TicketSummary, *, now: datetime) -> WatchRow:
        """Build a row from a pending ticket with no active session."""
        return cls(
            client=ticket.client,
            ticket_id=ticket.ticket_id,
            queue_status=ticket.status,
            session_status=_DASH,
            idle_age=_format_elapsed(ticket.created_at, now),
            last_activity=str(ticket.created_at),
        )

    @classmethod
    def from_running_ticket(
        cls,
        ticket: TicketSummary,
        session: SessionSummary,
        *,
        now: datetime,
    ) -> WatchRow:
        """Build a row merging a running ticket with its active session."""
        return cls(
            client=ticket.client,
            ticket_id=ticket.ticket_id,
            queue_status=ticket.status,
            session_status=session.status,
            idle_age=_format_elapsed(session.started_at, now),
            last_activity=str(session.started_at),
            session_id=session.id,
            worktree_path=session.worktree_path,
        )


def _build_watch_rows(
    status: OrchestratorStatus,
    now: datetime,
) -> list[WatchRow]:
    """Build a flat list of WatchRow from an OrchestratorStatus snapshot."""
    rows: list[WatchRow] = []

    # One row per running session
    for sess in status.running_sessions:
        # Try to find a matching running ticket for this session's client
        matching_ticket: TicketSummary | None = None
        for t in status.pending_tickets:
            if t.client == sess.client and t.status == "running":
                matching_ticket = t
                break

        if matching_ticket is not None:
            rows.append(WatchRow.from_running_ticket(matching_ticket, sess, now=now))
        else:
            rows.append(WatchRow.from_session(sess, now=now))

    # Pending tickets not already covered by a session row
    session_clients_covered = {sess.client for sess in status.running_sessions}
    for ticket in status.pending_tickets:
        if ticket.status == "running" and ticket.client in session_clients_covered:
            continue
        if ticket.status != "running":
            rows.append(WatchRow.from_ticket(ticket, now=now))

    return rows


def render_watch_table(
    status: OrchestratorStatus,
    *,
    now: datetime,
    selected: int = 0,
    home: str = "",
) -> RenderableType:
    """Render a flat table of all active work (sessions + tickets).

    Args:
        status: Snapshot produced by :func:`orchestrator_status`.
        now: Reference time for elapsed calculations.
        selected: Index of the currently selected row (highlighted).
        home: Home directory string for shortening worktree paths.

    Returns:
        A rich renderable (Table or Text).
    """
    rows = _build_watch_rows(status, now)
    if not rows:
        return Text("No active work.", style="dim")

    table = Table(
        show_edge=True,
        pad_edge=True,
        expand=True,
        header_style="bold cyan",
    )
    table.add_column("CLIENT", no_wrap=True)
    table.add_column("TICKET", width=12, no_wrap=True)
    table.add_column("Q-STATUS", width=10, no_wrap=True)
    table.add_column("S-STATUS", width=10, no_wrap=True)
    table.add_column("PANE-CMD", overflow="fold")
    table.add_column("IDLE-AGE", width=8, justify="right", no_wrap=True)
    table.add_column("LAST-ACTIVITY", overflow="fold")
    table.add_column("COST", width=8, justify="right", no_wrap=True)

    for i, row in enumerate(rows):
        style = "bold reverse" if i == selected else ""
        table.add_row(
            row.client,
            row.ticket_id or _DASH,
            row.queue_status,
            row.session_status,
            row.pane_cmd,
            row.idle_age or _DASH,
            (
                _shorten_worktree(row.worktree_path, home)
                if row.worktree_path
                else row.last_activity
            ),
            row.total_cost_usd,
            style=style,
        )

    return table


def _key_listener_thread(key_queue: queue.SimpleQueue[str]) -> None:
    """Daemon thread target: reads raw keystrokes from stdin and queues them."""
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                readable, _, _ = _sel.select([sys.stdin], [], [], 0.1)
                if readable:
                    ch = sys.stdin.read(1)
                    key_queue.put(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except OSError:
        return


def _handle_action_key(
    key: str,
    rows: list[WatchRow],
    cursor: int,
    notice_queue: queue.SimpleQueue[str],
) -> None:
    """Run the side effect for an action key ('o', 'p', 'c').

    These keys never move the cursor, quit, or force a refresh; they only
    spawn an external process or enqueue an operator notice.
    """
    n = len(rows)

    if key == "o":
        if n > 0 and rows[cursor].worktree_path is not None:
            editor = os.environ.get("EDITOR", "vi")
            subprocess.run(
                [editor, str(rows[cursor].worktree_path)],
                check=False,
            )
        else:
            notice_queue.put("no worktree for this row")
        return

    if key == "p":
        if shutil.which("cw") is not None:
            subprocess.run(["cw", "queue", "peek"], check=False)
        else:
            notice_queue.put("cw not on PATH")
        return

    if key == "c":
        notice_queue.put("spawn-complete not available (obs ticket not yet landed)")


def _handle_key(
    key: str,
    rows: list[WatchRow],
    cursor: int,
    notice_queue: queue.SimpleQueue[str],
) -> tuple[int, bool, bool]:
    """Handle a single keypress.

    Returns:
        (new_cursor, should_quit, force_refresh)
    """
    n = len(rows)

    if key == "q":
        return cursor, True, False

    if key == "r":
        return cursor, False, True

    if key == "j":
        new_cursor = min(cursor + 1, n - 1) if n > 0 else 0
        return new_cursor, False, False

    if key == "k":
        new_cursor = max(cursor - 1, 0) if n > 0 else 0
        return new_cursor, False, False

    if key in ("o", "p", "c"):
        _handle_action_key(key, rows, cursor, notice_queue)

    return cursor, False, False


def watch_flat(
    *,
    interval: int = 5,
    console: Console | None = None,
    ticks: int | None = None,
    status_fn: Callable[[], OrchestratorStatus] | None = None,
    home: str = "",
    key_queue: queue.SimpleQueue[str] | None = None,
) -> None:
    """Render the flat watch table live, refreshing every *interval* seconds.

    Args:
        interval: Seconds between refreshes. Clamped to [1, 60].
        console: Optional :class:`Console` (tests may pass a recording one).
        ticks: If set, render this many frames and exit (deterministic test path).
            ``None`` runs interactively until 'q' or Ctrl-C.
        status_fn: Snapshot provider. Defaults to :func:`orchestrator_status`.
        home: Home directory string for shortening worktree paths.
        key_queue: Pre-populated key queue (for tests). If None, starts a
            raw-terminal listener thread.
    """
    interval = max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, interval))
    console = console or Console()
    provider = status_fn or orchestrator_status

    # Deterministic test path: render N frames and return.
    if ticks is not None:
        for _ in range(ticks):
            console.print(
                render_watch_table(
                    provider(),
                    now=datetime.now(UTC),
                    home=home,
                )
            )
        return

    # Interactive path.
    if key_queue is None:
        kq: queue.SimpleQueue[str] = queue.SimpleQueue()
        listener = threading.Thread(
            target=_key_listener_thread,
            args=(kq,),
            daemon=True,
        )
        listener.start()
    else:
        kq = key_queue

    notice_q: queue.SimpleQueue[str] = queue.SimpleQueue()
    cursor = 0
    last_refresh = 0.0
    status = provider()

    with Live(
        render_watch_table(status, now=datetime.now(UTC), home=home, selected=cursor),
        console=console,
        screen=True,
        refresh_per_second=4,
    ) as live:
        try:
            while True:
                time.sleep(0.25)
                now = datetime.now(UTC)
                t_now = now.timestamp()
                force_refresh = False

                # Drain key queue.
                while not kq.empty():
                    key = kq.get_nowait()
                    current_rows = _build_watch_rows(status, now)
                    cursor, should_quit, did_force = _handle_key(
                        key, current_rows, cursor, notice_q
                    )
                    if should_quit:
                        return
                    if did_force:
                        force_refresh = True

                if t_now - last_refresh >= interval or force_refresh:
                    status = provider()
                    last_refresh = t_now

                # Drain notices (discard — no inline status bar).
                while not notice_q.empty():
                    notice_q.get_nowait()

                live.update(
                    render_watch_table(
                        status,
                        now=now,
                        home=home,
                        selected=cursor,
                    )
                )
        except KeyboardInterrupt:
            return
