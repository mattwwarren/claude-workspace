"""The ``cw statusline render`` command (#1644).

Claude Code invokes this from a statusline command on every assistant message,
so it must be fast (local file reads only — no ``gh``, ``git``, network, or
subprocess) and it must never fail: R3 pins exit 0 on every path, including a
missing, malformed, or unknown-session ``focus.json``.

Deliberately carries no ``@handle_errors``. The broad ``except Exception``
below *is* the R3 guarantee — the same shape ``cw guard-cwd`` uses for its
must-never-crash hook, and the same reason: a statusline that raised on every
message would be strictly worse than one that occasionally prints nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from cw.cli._base import main
from cw.statusline import render_work_segment

_SESSION_ENV_VAR = "CLAUDE_CODE_SESSION_ID"


@main.group("statusline", help="Render Claude Code statusline segments.")
def statusline_group() -> None:
    """Statusline segment renderers, invoked by Claude Code, not by hand."""


@statusline_group.command("render", help="Render the work-summary segment.")
@click.option(
    "--session",
    default=None,
    help=f"Session id (default: ${_SESSION_ENV_VAR}).",
)
@click.option(
    "--cwd",
    default=None,
    help="Directory to resolve a client from (default: the process cwd).",
)
def statusline_render(session: str | None, cwd: str | None) -> None:
    """Print the work segment, or nothing at all. Always exits 0.

    Both options exist only to make the resolution inputs testable and
    overridable; the real call site passes neither.
    """
    try:
        session_id = session or os.environ.get(_SESSION_ENV_VAR) or None
        segment = render_work_segment(session_id, Path(cwd) if cwd else Path.cwd())
    except Exception:  # noqa: BLE001 — machine-invoked; must never crash.
        return
    # Nothing to say prints nothing — not a blank line.
    if segment:
        click.echo(segment)
