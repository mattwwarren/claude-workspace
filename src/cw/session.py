"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import click

from cw import zellij
from cw.config import get_client, load_clients, load_state, save_state
from cw.handoff import extract_resumption_prompt, find_handoffs_newer_than, find_latest_handoff
from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus

CW_SESSION = "cw"


def _ensure_zellij() -> None:
    """Verify zellij is installed."""
    if not zellij.is_installed():
        raise click.ClickException(
            "Zellij is not installed. Install it: https://zellij.dev/documentation/installation"
        )


def _ensure_session_running(client: ClientConfig) -> bool:
    """Ensure the cw Zellij session exists with this client's tab.

    Returns True if we created and attached (caller should stop), False if already running.
    """
    if not zellij.session_exists(CW_SESSION):
        layout_path = zellij.generate_layout(client)
        click.echo(f"Launching Zellij session '{CW_SESSION}' for {client.name}...")
        # This will take over the terminal - user lands directly in the session
        zellij.create_and_attach(CW_SESSION, layout_path)
        return True
    return False


def start_session(client_name: str, purpose: str) -> None:
    """Start or resume a Claude Code session for a client."""
    _ensure_zellij()
    client = get_client(client_name)
    state = load_state()

    # Check for existing backgrounded session
    existing = state.find_session(client_name, purpose)
    if existing and existing.status == SessionStatus.BACKGROUNDED:
        click.echo(f"Found backgrounded session: {existing.name}")
        resume_session(existing.name)
        return

    if existing and existing.status == SessionStatus.ACTIVE:
        click.echo(f"Session already active: {existing.name}")
        if zellij.in_zellij_session():
            zellij.go_to_tab(client_name)
            zellij.focus_pane(purpose)
        else:
            click.echo(f"Attach with: zellij attach {CW_SESSION}")
        return

    # Record the session before launching (so it persists even if we exec into zellij)
    session = Session(
        name=f"{client_name}/{purpose}",
        client=client_name,
        purpose=SessionPurpose(purpose),
        workspace_path=client.workspace_path,
        zellij_pane=purpose,
        zellij_tab=client_name,
    )
    state.sessions.append(session)
    save_state(state)

    click.echo(f"Started session: {session.name} (id: {session.id})")

    # Ensure Zellij is running - if it creates a new session, it attaches and we're done
    if _ensure_session_running(client):
        return  # User is now inside Zellij

    # Session exists already - navigate within it or tell user how to attach
    if zellij.in_zellij_session():
        zellij.go_to_tab(client_name)
        zellij.focus_pane(purpose)
        zellij.write_to_pane("claude\n")
    else:
        click.echo(f"Zellij session '{CW_SESSION}' is running. Attaching...")
        zellij.attach_session(CW_SESSION)


def background_session(session_name: str | None = None) -> None:
    """Background a session by triggering /session-done and recording the handoff."""
    state = load_state()

    if session_name:
        session = state.find_by_name_or_id(session_name)
    else:
        # Try to detect from current Zellij pane context
        active = state.active_sessions()
        if len(active) == 1:
            session = active[0]
        elif not active:
            raise click.ClickException("No active sessions to background.")
        else:
            names = ", ".join(s.name for s in active)
            raise click.ClickException(
                f"Multiple active sessions. Specify which one: {names}"
            )

    if session is None:
        raise click.ClickException(f"Session not found: {session_name}")

    if session.status != SessionStatus.ACTIVE:
        raise click.ClickException(f"Session {session.name} is not active (status: {session.status})")

    click.echo(f"Backgrounding session: {session.name}...")

    # Record mtime before injection so we can detect new handoffs
    handoffs_dir = session.workspace_path / ".handoffs"
    before_mtime = time.time()

    if zellij.in_zellij_session():
        # Inject /session-done into the pane
        zellij.go_to_tab(session.zellij_tab or session.client)
        zellij.focus_pane(session.zellij_pane or session.purpose)
        zellij.write_to_pane("/session-done\n")

        # Poll for handoff file (max 30s)
        click.echo("Waiting for handoff generation...")
        for _ in range(30):
            time.sleep(1)
            new_handoffs = find_handoffs_newer_than(session.workspace_path, before_mtime)
            if new_handoffs:
                session.last_handoff_path = new_handoffs[0]
                click.echo(f"Handoff saved: {new_handoffs[0]}")
                break
        else:
            click.echo("Warning: No handoff detected within 30s. Session marked as backgrounded anyway.")
    else:
        # Not in Zellij - try to find latest handoff
        latest = find_latest_handoff(session.workspace_path)
        if latest:
            session.last_handoff_path = latest
        click.echo("Not inside Zellij session. Marking as backgrounded without /session-done injection.")

    session.status = SessionStatus.BACKGROUNDED
    session.backgrounded_at = datetime.now(timezone.utc)
    save_state(state)
    click.echo(f"Session {session.name} backgrounded.")


def resume_session(session_name: str) -> None:
    """Resume a backgrounded session with its handoff context."""
    _ensure_zellij()
    state = load_state()

    session = state.find_by_name_or_id(session_name)
    if session is None:
        raise click.ClickException(f"Session not found: {session_name}")

    if session.status != SessionStatus.BACKGROUNDED:
        raise click.ClickException(
            f"Session {session.name} is not backgrounded (status: {session.status})"
        )

    # Extract resumption prompt from handoff
    prompt = None
    if session.last_handoff_path and session.last_handoff_path.exists():
        prompt = extract_resumption_prompt(session.last_handoff_path)
        if prompt:
            click.echo(f"Loaded resumption context from: {session.last_handoff_path}")
        else:
            click.echo("Warning: Could not extract resumption prompt from handoff.")
    else:
        click.echo("No handoff file available. Starting fresh session.")

    # Ensure client tab exists
    client = get_client(session.client)
    _ensure_session_running(client)

    session.status = SessionStatus.ACTIVE
    session.resumed_at = datetime.now(timezone.utc)
    save_state(state)

    click.echo(f"Resumed session: {session.name}")

    if zellij.in_zellij_session():
        zellij.go_to_tab(session.zellij_tab or session.client)
        zellij.focus_pane(session.zellij_pane or session.purpose)

        # Launch claude and inject resumption prompt
        zellij.write_to_pane("claude\n")
        if prompt:
            time.sleep(2)  # Wait for Claude to initialize
            zellij.write_to_pane(prompt + "\n")
    else:
        click.echo(f"Attach with: zellij attach {CW_SESSION}")
        if prompt:
            click.echo("\nResumption prompt:")
            click.echo(prompt)


def list_sessions() -> None:
    """Display all tracked sessions."""
    state = load_state()

    if not state.sessions:
        click.echo("No sessions tracked.")
        return

    # Table header
    click.echo(f"{'CLIENT':<18} {'PURPOSE':<10} {'STATUS':<14} {'ID':<10} {'SINCE'}")
    click.echo("-" * 70)

    for s in state.sessions:
        if s.status == SessionStatus.COMPLETED:
            continue

        if s.status == SessionStatus.ACTIVE:
            since = _relative_time(s.resumed_at or s.started_at)
        elif s.status == SessionStatus.BACKGROUNDED:
            since = _relative_time(s.backgrounded_at or s.started_at)
        else:
            since = _relative_time(s.started_at)

        click.echo(f"{s.client:<18} {s.purpose:<10} {s.status:<14} {s.id:<10} {since}")


def show_status() -> None:
    """Show a summary dashboard across all clients."""
    state = load_state()
    clients = load_clients()

    active = state.active_sessions()
    backgrounded = state.backgrounded_sessions()

    click.echo(f"Clients configured: {len(clients)}")
    click.echo(f"Active sessions:    {len(active)}")
    click.echo(f"Backgrounded:       {len(backgrounded)}")
    click.echo()

    if active:
        click.echo("Active:")
        for s in active:
            click.echo(f"  {s.name} (since {_relative_time(s.resumed_at or s.started_at)})")

    if backgrounded:
        click.echo("Backgrounded:")
        for s in backgrounded:
            handoff = f" handoff: {s.last_handoff_path.name}" if s.last_handoff_path else ""
            click.echo(f"  {s.name}{handoff}")


def _relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    now = datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m}m ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    d = seconds // 86400
    return f"{d}d ago"
