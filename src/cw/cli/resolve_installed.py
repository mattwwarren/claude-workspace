"""The ``cw resolve-installed`` command (#3662).

Prints the first candidate path that is a non-empty regular file, polling
briefly so a transient fast-forward gap in ``~/.claude`` is ridden out. The
fences in ``auto-dev-impl.md`` and ``auto-dev.md`` call it with their own
candidate lists in their own order, so the lookup policy stays where it is.

Fails closed: a miss exits 1 with nothing on stdout and every candidate's
state on stderr. A recovered hit prints the path on stdout and a recovery
note on stderr.
"""

from __future__ import annotations

import os

import click

from cw.cli._base import handle_errors, main
from cw.installed_resolve import (
    DEFAULT_WAIT_S,
    WAIT_ENV_VAR,
    format_miss,
    format_recovered,
    resolve_first_present,
)

__all__ = ["resolve_installed"]


@main.command(
    "resolve-installed",
    help=(
        "Print the first CANDIDATE that is a non-empty regular file, retrying "
        "briefly on a miss. Exits 1 with the per-candidate state on stderr."
    ),
)
@click.argument("candidates", nargs=-1, required=True)
@click.option(
    "--wait-s",
    "wait_s",
    type=click.FloatRange(min=0),
    default=DEFAULT_WAIT_S,
    envvar=WAIT_ENV_VAR,
    show_default=True,
    help="Total seconds to poll before giving up (0 means one attempt).",
)
@handle_errors
def resolve_installed(candidates: tuple[str, ...], wait_s: float) -> None:
    """Resolve CANDIDATES in order and print the first usable path."""
    resolution = resolve_first_present(candidates, wait_s=wait_s)
    home = os.environ.get("HOME", "")
    if resolution.path is None:
        msg = format_miss(resolution, home)
        raise click.ClickException(msg)
    if resolution.attempts > 1:
        click.echo(format_recovered(resolution, home), err=True)
    click.echo(resolution.path)
