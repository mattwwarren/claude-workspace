"""The ``cw preflight`` command (#2697).

Thin Click wrapper over :func:`cw.preflight.run_preflight`, which replaced the
``cw-smoke-test`` skill's standalone script: that script needed ``yaml`` and
``pydantic`` importable and failed under a bare ``python3``. As a ``cw``
command it runs wherever ``cw`` does, including an orchestrator's own linked
worktree with no ``.claude/cw-context.json``.
"""

from __future__ import annotations

import json
import sys

import click

from cw.cli._base import handle_errors, main
from cw.preflight import run_preflight

__all__ = ["preflight"]


@main.command("preflight", help="Run pre-flight checks for one ticket before dispatch.")
@click.argument("ticket")
@click.option(
    "--client",
    "-c",
    "client_name",
    required=True,
    help="Client whose repo, tracker and dev-queue are checked.",
)
@click.option(
    "--repo",
    default=None,
    help=(
        "GitHub repo as OWNER/NAME; overrides the repo derived from the "
        "client's origin remote."
    ),
)
@handle_errors
def preflight(ticket: str, client_name: str, repo: str | None) -> None:
    """Run pre-flight checks for TICKET against CLIENT before dispatch.

    Prints one JSON object (ok, ticket_id, client, repo, repo_root, tracker,
    checks) and exits 1 when any hard check failed. Soft checks only warn.
    See `cw doctor` and `cw dev-queue status` for the underlying state.
    """
    report, rc = run_preflight(ticket.lstrip("#"), client_name, repo)
    click.echo(json.dumps(report))
    sys.exit(rc)
