"""Live dashboard for the orchestrator subsystem.

Renders the output of :func:`cw.orchestrate.orchestrator_status` as a rich
:class:`rich.console.Group` that can be passed to a static :class:`Console`
for ``cw orchestrate status`` or wrapped in :class:`rich.live.Live` for
``cw orchestrate watch``.

Sessions are grouped by client (primary axis) so the dashboard matches
the mental model of existing ``cw status`` / ``cw list`` output.  Worktree
paths are surfaced as a column so contention between parallel sessions on
overlapping branches is visible without hiding the client axis.
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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cw.models import DEFAULT_LANE
from cw.orchestrate import (
    EventSummary,
    MonitoredPR,
    OrchestratorStatus,
    SessionSummary,
    TicketSummary,
    orchestrator_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from rich.console import RenderableType

_MIN_INTERVAL_SECONDS = 1
_MAX_INTERVAL_SECONDS = 60
_WORKTREE_DISPLAY_MAX = 40


class DetailLevel(StrEnum):
    """How much detail the dashboard should render per row."""

    COMPACT = "compact"
    DEFAULT = "default"
    VERBOSE = "verbose"


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


def _shorten_worktree(path_value: object, home: str) -> str:
    """Display a worktree path relative to ``$HOME`` and capped in length."""
    if path_value is None:
        return "—"
    as_str = str(path_value)
    if home and as_str.startswith(home):
        as_str = "~" + as_str[len(home) :]
    if len(as_str) > _WORKTREE_DISPLAY_MAX:
        keep = _WORKTREE_DISPLAY_MAX - 1
        as_str = "…" + as_str[-keep:]
    return as_str


def _group_by_client(
    sessions: Iterable[SessionSummary],
    tickets: Iterable[TicketSummary],
) -> list[str]:
    """Return client names sorted by most activity first, then alphabetically."""
    counts: dict[str, int] = {}
    for sess in sessions:
        counts[sess.client] = counts.get(sess.client, 0) + 1
    for ticket in tickets:
        counts[ticket.client] = counts.get(ticket.client, 0) + 1
    return sorted(counts, key=lambda c: (-counts[c], c))


def _sessions_table(
    sessions: list[SessionSummary],
    *,
    level: DetailLevel,
    now: datetime,
    home: str,
) -> RenderableType:
    """Render a table of running sessions for one client."""
    if not sessions:
        return Text("  (no running sessions)", style="dim")
    table = Table(
        show_edge=False,
        pad_edge=False,
        expand=True,
        header_style="bold cyan",
    )
    table.add_column("ID", width=10, no_wrap=True)
    table.add_column("PURPOSE", width=8)
    table.add_column("WORKTREE", overflow="fold")
    table.add_column("STAGE", width=28, no_wrap=True)
    if level is DetailLevel.VERBOSE:
        table.add_column("SURFACE", width=12, no_wrap=True)
    table.add_column("ELAPSED", width=8, justify="right", no_wrap=True)
    table.add_column("HEARTBEAT", width=10, justify="right", no_wrap=True)
    table.add_column("SENTINEL", width=22, no_wrap=True)
    for sess in sorted(sessions, key=lambda s: s.started_at):
        row: list[str] = [
            sess.id,
            sess.purpose,
            _shorten_worktree(sess.worktree_path, home),
            sess.last_stage or "—",
        ]
        if level is DetailLevel.VERBOSE:
            row.append(sess.surface_ref or "—")
        row.append(_format_elapsed(sess.started_at, now))
        if sess.transcript_age_seconds is not None:
            fake_start = now - timedelta(seconds=sess.transcript_age_seconds)
            row.append(_format_elapsed(fake_start, now))
        else:
            row.append("—")
        row.append(sess.paused_status or "—")
        table.add_row(*row)
    return table


def _tickets_table(
    tickets: list[TicketSummary],
    *,
    level: DetailLevel,
) -> RenderableType:
    """Render a table of pending tickets for one client."""
    if not tickets:
        return Text("  (no pending tickets)", style="dim")
    table = Table(
        show_edge=False,
        pad_edge=False,
        expand=True,
        header_style="bold cyan",
    )
    table.add_column("TICKET", width=14, no_wrap=True)
    table.add_column("PRIORITY", width=8, justify="right")
    if level is DetailLevel.VERBOSE:
        table.add_column("SCOPE", overflow="fold")
    for ticket in sorted(
        tickets,
        key=lambda t: (-t.priority, t.created_at),
    ):
        raw = (
            ticket.ticket_id
            if ticket.lane == DEFAULT_LANE
            else f"{ticket.ticket_id} [{ticket.lane}]"
        )
        ticket_id_display = markup_escape(raw)
        row = [ticket_id_display, str(ticket.priority)]
        if level is DetailLevel.VERBOSE:
            row.append(ticket.scope_hint or "—")
        table.add_row(*row)
    return table


def _prs_table(prs: list[MonitoredPR], *, level: DetailLevel) -> RenderableType:
    """Render a table of monitored PRs for one client."""
    if not prs:
        return Text("  (no monitored PRs)", style="dim")
    table = Table(
        show_edge=False,
        pad_edge=False,
        expand=True,
        header_style="bold cyan",
    )
    table.add_column("PR", width=10, no_wrap=True)
    table.add_column("ROLE", width=8, no_wrap=True)
    table.add_column("STATUS", overflow="fold")
    table.add_column("CI", width=10, no_wrap=True)
    if level is DetailLevel.VERBOSE:
        table.add_column("MERGEABLE", width=10, no_wrap=True)
        table.add_column("UNRESOLVED", width=10, justify="right")
    for pr in sorted(prs, key=lambda p: (p.repo, p.pr_number)):
        row = [
            f"{pr.repo}#{pr.pr_number}",
            pr.role,
            pr.status,
            pr.ci_status or "—",
        ]
        if level is DetailLevel.VERBOSE:
            if pr.mergeable is True:
                mergeable_str = "✓"
            elif pr.mergeable is False:
                mergeable_str = "✗"
            else:
                mergeable_str = "—"
            row.append(mergeable_str)
            row.append(str(pr.unresolved_threads))
        table.add_row(*row)
    return table


def _client_panel(
    client: str,
    *,
    sessions: list[SessionSummary],
    tickets: list[TicketSummary],
    prs: list[MonitoredPR],
    level: DetailLevel,
    now: datetime,
    home: str,
) -> Panel:
    """Build a panel with sessions, tickets, and PRs for a single client."""
    if level is DetailLevel.COMPACT:
        body: RenderableType = Text(
            f"  running: {len(sessions)}   pending: {len(tickets)}   PRs: {len(prs)}",
        )
    else:
        body = Group(
            Text("Running sessions", style="bold"),
            _sessions_table(sessions, level=level, now=now, home=home),
            Text(""),
            Text("Pending tickets", style="bold"),
            _tickets_table(tickets, level=level),
            Text(""),
            Text("Monitored PRs", style="bold"),
            _prs_table(prs, level=level),
        )
    return Panel(body, title=f"[b]{client}[/b]", title_align="left")


class AttentionRow(BaseModel):
    """One row in the deduped attention digest."""

    key: str
    session_counts: dict[str, int]  # paused_status -> distinct session count


def _aggregate_attention_events(events: list[EventSummary]) -> list[AttentionRow]:
    """Aggregate SESSION_NEEDS_ATTENTION events into deduped attention rows.

    Dedup key: (session_id, paused_status) — each unique pair counted once.
    Grouping key: ticket_id when present in payload, else client.
    Returns one AttentionRow per distinct grouping key, sorted by key.
    """
    # group_key -> paused_status -> set of session_ids
    grouped: dict[str, dict[str, set[str]]] = {}

    for event in events:
        session_id = event.payload.get("session_id")
        paused_status = event.payload.get("paused_status")
        if not session_id or not paused_status:
            continue
        ticket_id = event.payload.get("ticket_id")
        client = event.payload.get("client")
        group_key = str(ticket_id) if ticket_id else (str(client) if client else "")
        if not group_key:
            continue
        by_status = grouped.setdefault(group_key, {})
        by_status.setdefault(str(paused_status), set()).add(str(session_id))

    return [
        AttentionRow(
            key=k,
            session_counts={ps: len(sids) for ps, sids in conditions.items()},
        )
        for k, conditions in sorted(grouped.items())
    ]


def _events_panel(
    events: list[EventSummary],
    *,
    attention_events: list[EventSummary] | None = None,
    level: DetailLevel,
) -> Panel:
    """Render the last-N events panel, with attention digest above raw events."""
    parts: list[RenderableType] = []

    attention_rows = _aggregate_attention_events(attention_events or [])
    if attention_rows:
        n = len(attention_rows)
        attn_lines: list[RenderableType] = [
            Text.from_markup(f"[b yellow]⚠ Attention ({n})[/b yellow]"),
        ]
        for row in attention_rows:
            segments = " · ".join(
                f"{count} {status}"
                for status, count in sorted(row.session_counts.items())
            )
            key_part = f"  [yellow]{markup_escape(row.key)}[/yellow]"
            seg_part = markup_escape(segments)
            attn_lines.append(
                Text.from_markup(f"{key_part} ⚠ {seg_part} · needs attention")
            )
        parts.append(Group(*attn_lines))
        parts.append(Text(""))

    if not events:
        parts.append(Text("(no recent events)", style="dim"))
    else:
        table = Table(show_edge=False, pad_edge=False, expand=True, show_header=False)
        table.add_column("TIME", width=8, no_wrap=True, style="dim")
        table.add_column("TYPE", width=22, no_wrap=True, style="magenta")
        table.add_column("PAYLOAD", overflow="fold")
        for event in events:
            timestamp = event.created_at.astimezone().strftime("%H:%M:%S")
            if level is DetailLevel.VERBOSE:
                payload_text = ", ".join(f"{k}={v}" for k, v in event.payload.items())
            else:
                payload_text = _summarise_event_payload(event.payload)
            table.add_row(timestamp, event.type, payload_text or "—")
        parts.append(table)

    body: RenderableType = Group(*parts) if len(parts) > 1 else parts[0]
    title = f"[b]Recent events[/b] ({len(events)})"
    return Panel(body, title=title, title_align="left")


def _summarise_event_payload(payload: dict[str, object]) -> str:
    """One-line payload summary: prefer repo/pr_number/session_id."""
    parts: list[str] = []
    repo = payload.get("repo")
    if repo:
        pr = payload.get("pr_number")
        parts.append(f"{repo}#{pr}" if pr is not None else str(repo))
    session_id = payload.get("session_id")
    if session_id:
        parts.append(f"session={session_id}")
    reason = payload.get("reason")
    if reason:
        parts.append(str(reason))
    return " ".join(parts)


def _header(status: OrchestratorStatus, *, level: DetailLevel) -> Text:
    """Top-line header with generation time and detail level."""
    local_ts = status.generated_at.astimezone().strftime("%H:%M:%S")
    return Text.from_markup(
        f"[b]cw orchestrate[/b]  [dim]generated {local_ts} · level={level.value}[/dim]",
    )


def render_dashboard(
    status: OrchestratorStatus,
    *,
    level: DetailLevel = DetailLevel.DEFAULT,
    client_filter: str | None = None,
    now: datetime | None = None,
    home: str = "",
) -> RenderableType:
    """Build the full dashboard renderable from an OrchestratorStatus.

    Args:
        status: Snapshot produced by :func:`orchestrator_status`.
        level: COMPACT / DEFAULT / VERBOSE.
        client_filter: If set, only show this client.
        now: Reference time for elapsed calculations.  Defaults to
            ``datetime.now(UTC)`` — override in tests for determinism.
        home: Home directory string used to shorten worktree paths.

    Returns:
        A rich renderable suitable for :class:`Console.print` or
        :class:`Live`.
    """
    now = now or datetime.now(UTC)
    clients = _group_by_client(status.running_sessions, status.pending_tickets)
    if client_filter is not None:
        clients = [c for c in clients if c == client_filter]

    panels: list[RenderableType] = [_header(status, level=level)]
    if not clients:
        panels.append(
            Panel(
                Text("No active clients.", style="dim"),
                title="[b]Clients[/b]",
                title_align="left",
            ),
        )
    for client in clients:
        sessions = [s for s in status.running_sessions if s.client == client]
        tickets = [t for t in status.pending_tickets if t.client == client]
        prs = _prs_for_client(status.monitored_prs, client, status.running_sessions)
        panels.append(
            _client_panel(
                client,
                sessions=sessions,
                tickets=tickets,
                prs=prs,
                level=level,
                now=now,
                home=home,
            ),
        )

    panels.append(
        _events_panel(
            status.recent_events,
            attention_events=status.attention_events,
            level=level,
        )
    )
    return Group(*panels)


def _prs_for_client(
    prs: list[MonitoredPR],
    client: str,
    sessions: list[SessionSummary],
) -> list[MonitoredPR]:
    """Filter PRs to those whose repo matches a session's name prefix.

    Falls back to including every PR when no sessions exist for the client,
    so the dashboard still surfaces PRs before any session has been spawned.
    """
    if not sessions:
        return prs
    session_clients = {s.client for s in sessions if s.client == client}
    if not session_clients:
        return []
    return prs


def watch(
    *,
    interval: int = 2,
    client_filter: str | None = None,
    level: DetailLevel = DetailLevel.DEFAULT,
    console: Console | None = None,
    ticks: int | None = None,
    status_fn: Callable[[], OrchestratorStatus] | None = None,
    home: str = "",
) -> None:
    """Render the dashboard live, refreshing every *interval* seconds.

    Args:
        interval: Seconds between refreshes.  Clamped to [1, 60].
        client_filter: If set, only render the named client.
        level: Detail level for rendered tables.
        console: Optional :class:`Console` (tests may pass a recording one).
        ticks: If set, render this many frames and exit — used by tests.
            ``None`` runs until the user hits Ctrl-C.
        status_fn: Snapshot provider.  Defaults to
            :func:`orchestrator_status`; tests inject fakes.
        home: Home directory string used to shorten worktree paths.
    """
    interval = max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, interval))
    console = console or Console()
    provider = status_fn or orchestrator_status

    def _build() -> RenderableType:
        return render_dashboard(
            provider(),
            level=level,
            client_filter=client_filter,
            home=home,
        )

    if ticks is not None:
        # Deterministic test path: render N frames directly.
        for _ in range(ticks):
            console.print(_build())
        return

    with Live(_build(), console=console, screen=True, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(_build())
        except KeyboardInterrupt:
            return


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
