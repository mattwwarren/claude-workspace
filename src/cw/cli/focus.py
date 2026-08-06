"""The ``cw focus`` command group — set/clear/show a session's work pointer (#1644).

``cw focus set <client>[/<lane>]`` tells ``cw statusline render`` which client
(and optionally which lane) this Claude Code session is working on. The session
id comes from ``--session`` or ``$CLAUDE_CODE_SESSION_ID``.

Unlike ``cw statusline render``, these are operator-facing commands and are NOT
subject to R3's never-fail contract: an unresolvable session id, an unknown
client, or an undeclared lane is a plain ``CwError``, surfaced through
``handle_errors`` exactly as ``cw lane pause``/``resume`` do.
"""

from __future__ import annotations

import os

import click

from cw.cli._base import SESSION_ENV_VAR, handle_errors, main
from cw.config import get_client
from cw.events import record_event
from cw.exceptions import CwError
from cw.focus import clear_focus, get_focus, set_focus
from cw.models import OrchestratorEventType

_SESSION_OPTION_HELP = f"Session id (default: ${SESSION_ENV_VAR})."


def _resolve_session_id(session: str | None) -> str:
    """Return the explicit ``--session`` value, else the env default."""
    session_id = session or os.environ.get(SESSION_ENV_VAR) or ""
    if not session_id:
        msg = (
            "No session id: pass --session <id> or set"
            f" ${SESSION_ENV_VAR} in the environment."
        )
        raise CwError(msg)
    return session_id


def _parse_target(target: str) -> tuple[str, str | None]:
    """Split ``client`` / ``client/lane`` into its validated parts.

    Validation mirrors ``lane_pause``/``lane_resume``: the client must exist in
    ``clients.yaml`` and the lane, when given, must be one it declares.
    """
    client, _, lane_raw = target.partition("/")
    lane = lane_raw or None
    client_cfg = get_client(client)
    if lane is not None and lane not in client_cfg.lane_names:
        msg = f"Lane '{lane}' is not declared for client '{client}'."
        raise CwError(msg)
    return client, lane


def _focus_label(client: str, lane: str | None) -> str:
    return client if lane is None else f"{client}/{lane}"


@main.group("focus", help="Set or show what this session is working on.")
def focus_group() -> None:
    """Manage the session-scoped focus pointer read by ``cw statusline``."""


@focus_group.command("set", help="Point this session at a client (and lane).")
@click.argument("target")
@click.option("--session", default=None, help=_SESSION_OPTION_HELP)
@handle_errors
def focus_set(target: str, session: str | None) -> None:
    """Focus this session on TARGET, given as ``client`` or ``client/lane``."""
    session_id = _resolve_session_id(session)
    client, lane = _parse_target(target)
    set_focus(session_id, client, lane)
    # Payload fields come from the already-resolved CLI values, never from a
    # re-read of the focus store we just wrote.
    record_event(
        OrchestratorEventType.FOCUS_SET,
        {"session_id": session_id, "client": client, "lane": lane},
    )
    click.echo(f"Focus for session '{session_id}': {_focus_label(client, lane)}")


@focus_group.command("clear", help="Clear this session's focus.")
@click.option("--session", default=None, help=_SESSION_OPTION_HELP)
@handle_errors
def focus_clear(session: str | None) -> None:
    """Drop this session's focus entry. Idempotent."""
    session_id = _resolve_session_id(session)
    # Captured before the delete so the event can report what was cleared. The
    # emit is unconditional, mirroring lane pause/resume — neither checks prior
    # state before recording the operator's intent.
    prior = get_focus(session_id)
    clear_focus(session_id)
    record_event(
        OrchestratorEventType.FOCUS_CLEARED,
        {
            "session_id": session_id,
            "client": prior.client if prior else None,
            "lane": prior.lane if prior else None,
        },
    )
    click.echo(f"Cleared focus for session '{session_id}'.")


@focus_group.command("show", help="Show this session's focus.")
@click.option("--session", default=None, help=_SESSION_OPTION_HELP)
@handle_errors
def focus_show(session: str | None) -> None:
    """Print this session's focus. Read-only: emits no event, takes no lock."""
    session_id = _resolve_session_id(session)
    entry = get_focus(session_id)
    if entry is None:
        click.echo(f"No focus set for session '{session_id}'.")
        return
    label = _focus_label(entry.client, entry.lane)
    click.echo(f"Focus for session '{session_id}': {label}")
