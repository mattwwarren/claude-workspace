"""The ``cw tests-prepare`` command (#2179).

Prints the ``tests.prepare`` command of ROOT's ``.claude/project-config.yaml``
so Step 2.5 gate 3 can run it in the clean gate worktree before the claimed
test command. Prints nothing when the key is unset or blank. Fails closed: a
malformed value exits 1 with nothing on stdout, and a ROOT that does not exist
exits 2 (Click usage error).
"""

from __future__ import annotations

from pathlib import Path

import click

from cw.cli._base import handle_errors, main
from cw.project_config import PrepareConfigError, read_tests_prepare

__all__ = ["tests_prepare"]


@main.command(
    "tests-prepare",
    help=(
        "Print the tests.prepare command from ROOT's .claude/project-config.yaml, "
        "or nothing when it is unset. "
        "Exits 1 when the value is malformed (not a string) or the file is not "
        "valid UTF-8, and 2 when ROOT does not exist."
    ),
)
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@handle_errors
def tests_prepare(root: Path) -> None:
    """Echo the tests.prepare command of ROOT, or nothing when it is unset."""
    try:
        command = read_tests_prepare(root)
    except PrepareConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if command is not None:
        click.echo(command)
