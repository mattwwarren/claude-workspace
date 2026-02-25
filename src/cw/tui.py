"""Rich TUI dashboard for claude-workspace using Textual."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
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
from cw.models import CwState, QueueItemStatus, SessionStatus
from cw.plan import PlanSummary, find_plan_files, parse_plan
from cw.queue import load_queue

if TYPE_CHECKING:
    from cw.history import HistoryEvent
    from cw.models import ClientConfig, QueueStore, Session


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
            ListItem(Static(name), id=f"client-{name}") for name in sorted(clients)
        ]
        super().__init__(*items)


class SessionTable(DataTable[str]):
    """Table showing sessions for the selected client."""

    def __init__(self) -> None:
        super().__init__()
        self.cursor_type = "row"


class QueuePanel(DataTable[str]):
    """Table showing pending/running queue items for the selected client."""

    def __init__(self) -> None:
        super().__init__()
        self.cursor_type = "row"


class ActivityFeed(RichLog):
    """Log of recent history events for the selected client."""


class PlanPanel(Static):
    """Panel showing plan progress for the selected client."""


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


class ConfirmScreen(ModalScreen[bool]):
    """Modal confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #confirm-dialog Static {
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }
    #confirm-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self._message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class SessionDetailScreen(ModalScreen[None]):
    """Modal showing full details of a session."""

    DEFAULT_CSS = """
    SessionDetailScreen {
        align: center middle;
    }
    #detail-dialog {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    #detail-dialog Static {
        width: 100%;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        s = self._session
        lines = [
            f"[bold]{s.name}[/bold]",
            "",
            f"  ID:       {s.id}",
            f"  Purpose:  {s.purpose}",
            f"  Status:   {_format_status(s.status)}",
            f"  Origin:   {s.origin}",
            f"  Branch:   {s.branch or '[dim]none[/dim]'}",
            f"  Worktree: {s.worktree_path or '[dim]none[/dim]'}",
            f"  Handoff:  {s.last_handoff_path or '[dim]none[/dim]'}",
            "",
            f"  Started:       {relative_time(s.started_at)}",
        ]
        if s.backgrounded_at:
            lines.append(f"  Backgrounded:  {relative_time(s.backgrounded_at)}")
        if s.resumed_at:
            lines.append(f"  Resumed:       {relative_time(s.resumed_at)}")
        if s.completed_at:
            lines.append(f"  Completed:     {relative_time(s.completed_at)}")
            reason = s.completed_reason or "[dim]unknown[/dim]"
            lines.append(f"  Reason:        {reason}")
        lines.append("")
        lines.append("[dim]Press Escape to close[/dim]")

        with Vertical(id="detail-dialog"):
            yield Static("\n".join(lines))

    async def action_dismiss(self, result: None = None) -> None:
        await super().action_dismiss(result)


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
    #center-panel { width: 50%; height: 100%; }
    SessionTable { height: 60%; }
    QueuePanel { height: 40%; border-top: solid $primary; }
    #right-panel { width: 30%; border-left: solid $primary; height: 100%; }
    ActivityFeed { height: 1fr; }
    PlanPanel {
        height: auto;
        max-height: 40%;
        border-top: solid $primary;
        padding: 0 1;
    }
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
        Binding("e", "expand_session", "Expand"),
        Binding("?", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._clients: dict[str, ClientConfig] = {}
        self._selected_client: str | None = None
        self._sessions: list[Session] = []
        self._state: CwState = CwState()
        self._queue: QueueStore | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ClientList(self._clients)
            with Vertical(id="center-panel"):
                yield SessionTable()
                yield QueuePanel()
            with Vertical(id="right-panel"):
                yield ActivityFeed()
                yield PlanPanel("")
        yield StatusLine("Loading...")
        yield Footer()

    def on_mount(self) -> None:
        """Load initial data and set up refresh timer."""
        table = self.query_one(SessionTable)
        table.add_columns(
            "Purpose",
            "Status",
            "Origin",
            "Since",
            "Branch",
            "Handoff",
        )
        queue_table = self.query_one(QueuePanel)
        queue_table.add_columns("ID", "Status", "Purpose", "Description")
        self._load_clients()
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
        self._refresh_sidebar()
        self._refresh_queue()
        self._refresh_activity()
        self._refresh_plans()
        self._refresh_status()

    def _refresh_sessions(self) -> None:
        """Update the session table for the selected client."""
        table = self.query_one(SessionTable)
        table.clear()

        if not self._selected_client:
            return

        self._state = load_state()
        self._sessions = [
            s
            for s in self._state.sessions
            if s.client == self._selected_client and s.status != SessionStatus.COMPLETED
        ]

        for s in self._sessions:
            status_display = _format_status(s.status)
            origin = _format_origin(s.origin)
            since = _session_time(s)
            branch = s.branch or ""
            handoff = "[dim]handoff[/dim]" if s.last_handoff_path else ""
            table.add_row(
                s.purpose,
                status_display,
                origin,
                since,
                branch,
                handoff,
                key=s.id,
            )

    def _refresh_sidebar(self) -> None:
        """Update client sidebar with session count badges."""
        sidebar = self.query_one(ClientList)
        for item in sidebar.query(ListItem):
            if not item.id or not item.id.startswith("client-"):
                continue
            client_name = item.id.removeprefix("client-")
            active = bg = 0
            if hasattr(self, "_state"):
                for s in self._state.sessions:
                    if s.client != client_name:
                        continue
                    if s.status == SessionStatus.ACTIVE:
                        active += 1
                    elif s.status == SessionStatus.BACKGROUNDED:
                        bg += 1
            label = client_name
            if active or bg:
                label += f" [dim][A:{active} B:{bg}][/dim]"
            static = item.query_one(Static)
            static.update(label)

    def _refresh_queue(self) -> None:
        """Update the queue panel for the selected client."""
        table = self.query_one(QueuePanel)
        table.clear()
        self._queue = None

        if not self._selected_client:
            return

        store = load_queue(self._selected_client)
        self._queue = store
        active_items = [
            i
            for i in store.items
            if i.status in (QueueItemStatus.PENDING, QueueItemStatus.RUNNING)
        ]

        for item in active_items:
            status_display = _format_queue_status(item.status)
            desc = item.task.description
            max_desc = 35
            if len(desc) > max_desc:
                desc = desc[: max_desc - 3] + "..."
            table.add_row(
                item.id[:8],
                status_display,
                item.task.purpose,
                desc,
                key=item.id,
            )

    def _refresh_activity(self) -> None:
        """Update the activity feed for the selected client."""
        feed = self.query_one(ActivityFeed)
        feed.clear()

        if not self._selected_client:
            return

        events = load_history(self._selected_client, limit=50)
        for event in events:
            feed.write(_format_event(event))

    def _refresh_plans(self) -> None:
        """Update the plan panel for the selected client."""
        panel = self.query_one(PlanPanel)

        if not self._selected_client or self._selected_client not in self._clients:
            panel.update("")
            return

        workspace = Path(self._clients[self._selected_client].workspace_path)
        plans = find_plan_files(workspace)
        if not plans:
            panel.update("[dim]No plans[/dim]")
            return

        lines: list[str] = []
        for plan_path in plans:
            try:
                summary = parse_plan(plan_path)
            except (OSError, ValueError):
                continue
            text = _format_plan_summary(summary)
            if text:
                lines.append(text)

        panel.update("\n".join(lines) if lines else "[dim]No active plans[/dim]")

    def _refresh_status(self) -> None:
        """Update the bottom status line."""
        if not self._selected_client:
            self.query_one(StatusLine).update("No clients configured")
            return

        active = sum(1 for s in self._sessions if s.status == SessionStatus.ACTIVE)
        bg = sum(1 for s in self._sessions if s.status == SessionStatus.BACKGROUNDED)

        parts = [f"Client: {self._selected_client}"]
        parts.append(f"Active: {active}")
        parts.append(f"Bg: {bg}")

        if self._queue:
            pending = len(self._queue.pending())
            if pending > 0:
                parts.append(f"Queue: {pending} pending")

        # Plan progress from the plan panel content
        panel = self.query_one(PlanPanel)
        panel_text = str(panel.render())
        has_plan = (
            panel_text
            and "No plans" not in panel_text
            and "No active" not in panel_text
        )
        if has_plan:
            # Extract progress from first plan line (e.g. "[1/3] 33%")
            match = re.search(r"\[(\d+)/(\d+)\]", panel_text)
            if match:
                parts.append(f"Plan: {match.group(1)}/{match.group(2)}")

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

    def action_done_session(self) -> None:
        """Mark the selected session as done (with confirmation)."""
        session = self._get_selected_session()
        if session is None:
            return

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._do_done_session(session.name)

        self.push_screen(
            ConfirmScreen(f"Mark [bold]{session.name}[/bold] as done?"),
            _on_confirm,
        )

    @work(thread=True)
    def _do_done_session(self, session_name: str) -> None:
        """Execute the done command after confirmation."""
        subprocess.run(
            ["cw", "done", session_name],
            check=False,
            capture_output=True,
        )
        self.call_from_thread(self._refresh_data)

    def action_expand_session(self) -> None:
        """Show full details of the selected session."""
        session = self._get_selected_session()
        if session is None:
            return
        self.push_screen(SessionDetailScreen(session))

    def action_help(self) -> None:
        """Show help overlay."""
        self.notify(
            "Keys: r=Resume, b=Background, d=Done, e=Expand, q=Quit",
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


def _format_queue_status(status: str) -> str:
    """Format queue item status with color markers."""
    colors = {
        "pending": "[yellow]pending[/yellow]",
        "running": "[green]running[/green]",
        "completed": "[dim]completed[/dim]",
        "failed": "[red]failed[/red]",
    }
    return colors.get(status, status)


def _format_origin(origin: str) -> str:
    """Format session origin with badge markers."""
    badges = {
        "delegate": "[cyan][delegate][/cyan]",
        "daemon": "[magenta][daemon][/magenta]",
    }
    return badges.get(origin, "")


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


def _format_plan_summary(summary: PlanSummary) -> str:
    """Format a plan summary as Rich markup for the TUI panel.

    Returns an empty string for completed plans (100%) or plans with no tasks.
    """
    done, total = summary.progress
    if total == 0:
        return ""
    pct = int(done / total * 100)
    if pct == 100:
        return ""

    lines = [f"[bold]{summary.title}[/bold] [{done}/{total}] {pct}%"]
    for phase in summary.phases:
        p_done, p_total = phase.progress
        if p_total == 0:
            continue
        if p_done == p_total:
            label = "[green]Done[/green]"
        else:
            label = f"[yellow]{p_done}/{p_total}[/yellow]"
        lines.append(f"  {phase.name}: {label}")
    return "\n".join(lines)


def run_dashboard() -> None:
    """Launch the TUI dashboard."""
    app = CwDashboard()
    app.run()
