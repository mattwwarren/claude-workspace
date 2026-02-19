"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import click

from cw import __version__
from cw.models import SessionPurpose


@click.group()
@click.version_option(version=__version__, prog_name="cw")
def main() -> None:
    """Claude Workspace - multi-session orchestrator for Claude Code."""


@main.command()
@click.argument("client")
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default="impl",
    help="Session purpose.",
)
def start(client: str, purpose: str) -> None:
    """Start or resume a Claude Code session for a client."""
    from cw.session import start_session

    start_session(client, purpose)


@main.command()
def bg() -> None:
    """Background the current session (auto-handoff)."""
    from cw.session import background_session

    background_session()


@main.command()
@click.argument("session_name")
def resume(session_name: str) -> None:
    """Resume a backgrounded session."""
    from cw.session import resume_session

    resume_session(session_name)


@main.command(name="list")
def list_sessions() -> None:
    """List all sessions across clients."""
    from cw.session import list_sessions as _list_sessions

    _list_sessions()


@main.command()
@click.argument("client")
def switch(client: str) -> None:
    """Switch to a client's Zellij tab."""
    from cw.zellij import go_to_tab

    go_to_tab(client)


@main.command()
def status() -> None:
    """Show status dashboard across all clients."""
    from cw.session import show_status

    show_status()


@main.command()
def config() -> None:
    """Show current configuration."""
    from cw.config import show_config

    show_config()
