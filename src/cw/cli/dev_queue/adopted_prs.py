"""``cw dev-queue adopted-prs`` -- sibling PRs carried by live adopted rows (#2949).

Read-only. Backs the finalize Step 4a merge-gate inventory: a PR adopted via
RFC 0016 may sit on a non-prefix branch and be authored by someone else, so the
``<branch-prefix>/*`` inventory cannot see it. This command names those PRs
using the same liveness predicate (:func:`cw.dev_queue.adoption.is_live_row`)
and branch seam (:func:`cw.models.branch.resolve_feature_branch`) the adoption
policy already uses, so the policy lives once, in tested Python.

Output contract: one line per matching row, ``<number> <branch>``, where
``<number>`` is the PR number parsed from ``adopted_pr_url`` and ``-`` replaces
it when that URL does not parse. Empty output and exit 0 when nothing matches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.config import load_clients
from cw.dev_queue import load_dev_queue
from cw.dev_queue.adoption import is_live_row
from cw.models.branch import resolve_feature_branch
from cw.pr_hydrate import _parse_pr_url

from ._group import dev_queue

if TYPE_CHECKING:
    from cw.models import TicketTask
    from cw.models.client import ClientConfig

_UNPARSED_PR_NUMBER = "-"


def adopted_sibling_lines(
    tasks: list[TicketTask], client: str, client_cfg: ClientConfig | None
) -> list[str]:
    """``<number> <branch>`` for each live adopted row of *client*, queue order."""
    lines: list[str] = []
    for row in tasks:
        if row.client != client or row.adopted_pr_url is None:
            continue
        if not is_live_row(row):
            continue
        parsed = _parse_pr_url(row.adopted_pr_url)
        number = _UNPARSED_PR_NUMBER if parsed is None else str(parsed[1])
        branch = resolve_feature_branch(row.ticket_id, client_cfg, row)
        lines.append(f"{number} {branch}")
    return lines


@dev_queue.command(name="adopted-prs")
@click.option(
    "--client",
    "-c",
    required=True,
    help="Client whose live adopted rows to list.",
)
@handle_errors
def dev_queue_adopted_prs(client: str) -> None:
    """List PRs carried by live adopted rows, as ``<number> <branch>`` lines.

    Backs finalize Step 4a's merge-gate sibling inventory (RFC 0016, #2949).
    A row counts while it is live (see ``is_live_row``); the number is ``-``
    when the recorded ``adopted_pr_url`` does not parse. Read-only: emits no
    events and changes nothing.
    """
    queue = load_dev_queue()
    client_cfg = load_clients().get(client)
    for line in adopted_sibling_lines(queue.tasks, client, client_cfg):
        click.echo(line)
