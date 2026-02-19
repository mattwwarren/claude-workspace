"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, datetime

import click
from click.shell_completion import CompletionItem

from cw import __version__
from cw.exceptions import CwError
from cw.models import CwState, Session, SessionPurpose, SessionStatus


def handle_errors[F: Callable[..., object]](fn: F) -> F:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper  # type: ignore[return-value]


def _complete_client(
    ctx: click.Context,
    param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete client names from config."""
    from cw.config import load_clients

    return [
        CompletionItem(name)
        for name in load_clients()
        if name.startswith(incomplete)
    ]


def _complete_session(
    ctx: click.Context,
    param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete session names from backgrounded sessions."""
    from cw.config import load_state

    state = load_state()
    return [
        CompletionItem(s.name)
        for s in state.sessions
        if s.name.startswith(incomplete) and s.status != SessionStatus.COMPLETED
    ]


@click.group()
@click.version_option(version=__version__, prog_name="cw")
def main() -> None:
    """Claude Workspace - multi-session orchestrator for Claude Code."""


@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default="impl",
    help="Session purpose.",
)
@handle_errors
def start(client: str, purpose: str) -> None:
    """Start or resume a Claude Code session for a client."""
    from cw.session import start_session

    start_session(client, purpose)


@main.command()
@handle_errors
def bg() -> None:
    """Background the current session (auto-handoff)."""
    from cw.session import background_session

    background_session()


@main.command()
@click.argument("session_name", shell_complete=_complete_session)
@handle_errors
def resume(session_name: str) -> None:
    """Resume a backgrounded session."""
    from cw.session import resume_session

    resume_session(session_name)


@main.command(name="list")
@handle_errors
def list_sessions() -> None:
    """List all sessions across clients."""
    _display_sessions()


@main.command()
@click.argument("client", shell_complete=_complete_client)
@handle_errors
def switch(client: str) -> None:
    """Switch to a client's Zellij tab."""
    from cw.zellij import go_to_tab

    go_to_tab(client)


@main.command()
@handle_errors
def status() -> None:
    """Show status dashboard across all clients."""
    _display_status()


@main.command()
@click.argument(
    "target",
    type=click.Choice([e.value for e in SessionPurpose]),
)
@click.argument("message")
@click.option(
    "--from",
    "source",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Source session (for audit trail).",
)
@handle_errors
def hand(target: str, message: str, source: str | None) -> None:
    """Hand off a message to another session.

    Example: cw hand debt "Fix the ruff violations in session.py"
    """
    from cw.session import hand_to_session

    hand_to_session(target, message, source_purpose=source)


@main.command()
@handle_errors
def config() -> None:
    """Show current configuration."""
    from cw.config import show_config

    show_config()


def _relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    now = datetime.now(UTC)
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


def _display_sessions() -> None:
    """Display all tracked sessions."""
    from cw.config import load_state

    state = load_state()

    if not state.sessions:
        click.echo("No sessions tracked.")
        return

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


def _check_and_mark_dead_sessions(state: CwState) -> list[Session]:
    """Check active sessions for dead panes, mark them COMPLETED."""
    from cw import zellij
    from cw.config import save_state
    from cw.session import CW_SESSION

    if not zellij.session_exists(CW_SESSION):
        return []

    health = zellij.check_pane_health(session=CW_SESSION)
    if not health:
        return []

    dead: list[Session] = []
    for s in state.active_sessions():
        pane_name = s.zellij_pane or s.purpose
        if pane_name in health and not health[pane_name]:
            s.status = SessionStatus.COMPLETED
            dead.append(s)

    if dead:
        save_state(state)

    return dead


def _display_status() -> None:
    """Show a summary dashboard across all clients."""
    from cw.config import load_clients, load_state

    state = load_state()
    clients = load_clients()

    dead = _check_and_mark_dead_sessions(state)
    for s in dead:
        click.echo(f"Detected crashed session: {s.name} (marked completed)")

    active = state.active_sessions()
    backgrounded = state.backgrounded_sessions()

    click.echo(f"Clients configured: {len(clients)}")
    click.echo(f"Active sessions:    {len(active)}")
    click.echo(f"Backgrounded:       {len(backgrounded)}")
    click.echo()

    if active:
        click.echo("Active:")
        for s in active:
            since = _relative_time(s.resumed_at or s.started_at)
            click.echo(f"  {s.name} (since {since})")

    if backgrounded:
        click.echo("Backgrounded:")
        for s in backgrounded:
            handoff = (
                f" handoff: {s.last_handoff_path.name}"
                if s.last_handoff_path
                else ""
            )
            click.echo(f"  {s.name}{handoff}")


_COMPLETION_SCRIPTS = {
    "bash": 'eval "$(_CW_COMPLETE=bash_source cw)"',
    "zsh": 'eval "$(_CW_COMPLETE=zsh_source cw)"',
    "fish": "_CW_COMPLETE=fish_source cw | source",
}


@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Output shell completion activation script.

    Add to your shell profile:

    \b
      # Bash (~/.bashrc)
      eval "$(_CW_COMPLETE=bash_source cw)"

    \b
      # Zsh (~/.zshrc)
      eval "$(_CW_COMPLETE=zsh_source cw)"

    \b
      # Fish (~/.config/fish/config.fish)
      _CW_COMPLETE=fish_source cw | source
    """
    # Output the activation one-liner for the user to add to their profile
    click.echo("# Add this to your shell profile:")
    click.echo(_COMPLETION_SCRIPTS[shell])
