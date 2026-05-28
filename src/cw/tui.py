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

import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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


def _format_elapsed(started_at: datetime, now: datetime) -> str:
    """Return a short ``HhMmSs``-style elapsed time string."""
    delta = max(now - started_at, now - now)  # Guard against clock skew.
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}m{seconds:02d}s"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
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
        row = [ticket.ticket_id, str(ticket.priority)]
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
    if level is DetailLevel.VERBOSE:
        table.add_column("UNRESOLVED", width=10, justify="right")
    for pr in sorted(prs, key=lambda p: (p.repo, p.pr_number)):
        row = [
            f"{pr.repo}#{pr.pr_number}",
            pr.role,
            pr.status,
        ]
        if level is DetailLevel.VERBOSE:
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


def _events_panel(
    events: list[EventSummary],
    *,
    level: DetailLevel,
) -> Panel:
    """Render the last-N events panel."""
    if not events:
        body: RenderableType = Text("(no recent events)", style="dim")
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
        body = table
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

    panels.append(_events_panel(status.recent_events, level=level))
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
