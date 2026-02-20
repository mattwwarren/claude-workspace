"""Rich TUI dashboard for claude-workspace using Textual."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from cw.cli import _relative_time as relative_time
from cw.config import load_clients, load_state
from cw.history import load_history
from cw.models import SessionStatus

if TYPE_CHECKING:
    from cw.history import HistoryEvent
    from cw.models import ClientConfig, Session


class ClientList(ListView):
    """Sidebar listing configured clients."""

    DEFAULT_CSS = """
    ClientList {
        width: 20%;
        border-right: solid $primary;
    }
    """

    def __init__(self, clients: dict[str, ClientConfig]) -> None:
        self._clients = clients
        items = [
            ListItem(Static(name), id=f"client-{name}")
            for name in sorted(clients)
        ]
        super().__init__(*items)


class SessionTable(DataTable[str]):
    """Table showing sessions for the selected client."""

    DEFAULT_CSS = """
    SessionTable {
        width: 50%;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.cursor_type = "row"


class ActivityFeed(RichLog):
    """Log of recent history events for the selected client."""

    DEFAULT_CSS = """
    ActivityFeed {
        width: 30%;
        border-left: solid $primary;
    }
    """


class StatusLine(Static):
    """Bottom status bar showing queue/daemon counts."""

    DEFAULT_CSS = """
    StatusLine {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """


class CwDashboard(App[None]):
    """Interactive dashboard for claude-workspace."""

    TITLE = "cw dashboard"
    # Inlined to avoid packaging issues with uv tool install (same
    # pattern as zellij.py's CLIENT_LAYOUT_TEMPLATE).
    DEFAULT_CSS = """
    Screen { layout: vertical; }
    Horizontal { height: 1fr; }
    Header { dock: top; }
    Footer { dock: bottom; }
    ClientList { width: 20%; border-right: solid $primary; height: 100%; }
    ClientList > ListItem { padding: 0 1; }
    SessionTable { width: 50%; height: 100%; }
    ActivityFeed { width: 30%; border-left: solid $primary; height: 100%; }
    StatusLine {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "resume_session", "Resume"),
        Binding("b", "background_session", "Background"),
        Binding("d", "done_session", "Done"),
        Binding("?", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._clients: dict[str, ClientConfig] = {}
        self._selected_client: str | None = None
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ClientList(self._clients)
            yield SessionTable()
            yield ActivityFeed()
        yield StatusLine("Loading...")
        yield Footer()

    def on_mount(self) -> None:
        """Load initial data and set up refresh timer."""
        self._load_clients()
        table = self.query_one(SessionTable)
        table.add_columns("Purpose", "Status", "Since", "Branch")
        self.set_interval(2.0, self._refresh_data)

    def _load_clients(self) -> None:
        """Load client configs from disk."""
        self._clients = load_clients()
        if self._clients:
            self._selected_client = sorted(self._clients)[0]
            self._refresh_data()

    def _refresh_data(self) -> None:
        """Poll state files and update all panels."""
        self._refresh_sessions()
        self._refresh_activity()
        self._refresh_status()

    def _refresh_sessions(self) -> None:
        """Update the session table for the selected client."""
        table = self.query_one(SessionTable)
        table.clear()

        if not self._selected_client:
            return

        state = load_state()
        self._sessions = [
            s for s in state.sessions
            if s.client == self._selected_client
            and s.status != SessionStatus.COMPLETED
        ]

        for s in self._sessions:
            status_display = _format_status(s.status)
            since = _session_time(s)
            branch = s.branch or ""
            table.add_row(s.purpose, status_display, since, branch, key=s.id)

    def _refresh_activity(self) -> None:
        """Update the activity feed for the selected client."""
        feed = self.query_one(ActivityFeed)
        feed.clear()

        if not self._selected_client:
            return

        events = load_history(self._selected_client, limit=50)
        for event in events:
            feed.write(_format_event(event))

    def _refresh_status(self) -> None:
        """Update the bottom status line."""
        if not self._selected_client:
            self.query_one(StatusLine).update("No clients configured")
            return

        active = sum(1 for s in self._sessions if s.status == SessionStatus.ACTIVE)
        bg = sum(1 for s in self._sessions if s.status == SessionStatus.BACKGROUNDED)

        parts = [f"Client: {self._selected_client}"]
        parts.append(f"Active: {active}")
        parts.append(f"Backgrounded: {bg}")

        self.query_one(StatusLine).update(" | ".join(parts))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle client selection."""
        if event.item.id and event.item.id.startswith("client-"):
            self._selected_client = event.item.id.removeprefix("client-")
            self._refresh_data()

    def _get_selected_session(self) -> Session | None:
        """Get the session selected in the table."""
        table = self.query_one(SessionTable)
        if table.cursor_row >= len(self._sessions):
            return None
        return self._sessions[table.cursor_row]

    # Actions shell out to `cw` CLI rather than calling functions directly.
    # This avoids entangling the TUI with session internals and click.echo
    # output that would corrupt the terminal. Acceptable cost: one subprocess
    # per user-initiated action.

    @work(thread=True)
    def action_resume_session(self) -> None:
        """Resume the selected backgrounded session."""
        session = self._get_selected_session()
        if session is None or session.status != SessionStatus.BACKGROUNDED:
            return
        subprocess.run(
            ["cw", "resume", session.name],
            check=False,
            capture_output=True,
        )
        self.call_from_thread(self._refresh_data)

    @work(thread=True)
    def action_background_session(self) -> None:
        """Background the selected active session."""
        session = self._get_selected_session()
        if session is None or session.status != SessionStatus.ACTIVE:
            return
        subprocess.run(
            ["cw", "bg"],
            check=False,
            capture_output=True,
        )
        self.call_from_thread(self._refresh_data)

    @work(thread=True)
    def action_done_session(self) -> None:
        """Mark the selected session as done."""
        session = self._get_selected_session()
        if session is None:
            return
        subprocess.run(
            ["cw", "done", session.name],
            check=False,
            capture_output=True,
        )
        self.call_from_thread(self._refresh_data)

    def action_help(self) -> None:
        """Show help overlay."""
        self.notify(
            "Keys: r=Resume, b=Background, d=Done, q=Quit",
            title="Help",
        )


def _format_status(status: str) -> str:
    """Format session status with color markers."""
    colors = {
        "active": "[green]active[/green]",
        "backgrounded": "[yellow]backgrounded[/yellow]",
        "completed": "[dim]completed[/dim]",
    }
    return colors.get(status, status)


def _session_time(session: Session) -> str:
    """Pick the best timestamp for a session and format as relative string."""
    dt = session.resumed_at or session.backgrounded_at or session.started_at
    return relative_time(dt)


def _format_event(event: HistoryEvent) -> str:
    """Format a history event for the activity feed."""
    ts = relative_time(event.timestamp)
    detail = f" - {event.detail}" if event.detail else ""
    session = f" [{event.session_name}]" if event.session_name else ""
    return f"{ts} {event.event_type}{session}{detail}"


def run_dashboard() -> None:
    """Launch the TUI dashboard."""
    app = CwDashboard()
    app.run()
