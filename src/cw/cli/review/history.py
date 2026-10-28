"""``cw review history`` — replay one ticket's review round by round (#2790).

Read-only, like ``cw review dispositions`` and ``cw dev-queue show``: the
unlocked ``load_dev_queue``/``load_state`` reads, the converge ledger and its
per-round verdict files, and two event-inbox reads (each under the inbox's own
short leaf lock). No state lock, no subprocess, no event recorded. The
collection and the pure build live in :mod:`cw.review_history`, the text in
:mod:`cw.review_history_render`.

Only two failures exit 1: no dev-queue row for the ticket, and a ticket queued
under several clients without ``-c``. Every data problem exits 0 and is named
under ``notes``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
import yaml

from cw.cli._base import handle_errors
from cw.config import load_state
from cw.dev_queue import _find_ticket, load_dev_queue
from cw.exceptions import CwError
from cw.review_history import build_review_history, collect_history_inputs
from cw.review_history_render import render_review_history
from cw.ticket_view import ViewSources, resolve_view_client

from ._group import review

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState


def _client_config(name: str) -> ClientConfig | None:
    """*name*'s effective config, or None when it is unconfigured or unreadable."""
    try:
        return ViewSources().effective_client(name)
    except (CwError, OSError, ValueError, yaml.YAMLError):
        return None


def _state_or_none() -> CwState | None:
    """The session state, or None when it cannot be read (noted by the builder)."""
    try:
        return load_state()
    except (CwError, OSError, ValueError):
        return None


@review.command(name="history")
@click.argument("ticket_id", metavar="TICKET")
@click.option(
    "--client",
    "-c",
    default=None,
    help="Client name. Default: the one client whose queue holds TICKET.",
)
@click.option(
    "--worktree",
    default=None,
    type=click.Path(path_type=Path, file_okay=False),
    help=(
        "The checkout that holds .cw/review-convergence.json. Overrides the "
        "automatic lookup (the row's worktree, its bound session's worktree, "
        "then the derived path). Unlike `cw review dispositions --worktree`, "
        "it is not a staleness baseline."
    ),
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Emit the ReviewHistory JSON (schema: `cw schema show review-history`).",
)
@handle_errors
def review_history(
    ticket_id: str, client: str | None, worktree: Path | None, output_json: bool
) -> None:
    """Replay TICKET's clean-mode review round by round (#2790).

    Read-only: no state lock, no subprocess, no event recorded. Prints one
    `key: value` per line in a fixed key order, every key always present (`-`
    when absent), nothing truncated, then one indented block per round:
    reviewers, reviewed sha, findings raised and how many were new, the
    severity split of the new ones, fixes dispatched, findings settled as
    reject or defer with their rationale, and wall time. Wall time runs from
    the previous round's record to this one, so it includes the fix cycle in
    between.

    Rounds come from the converge ledger in the ticket's worktree, the file
    `cw review converge status` reads. When that is absent, unreadable or
    owned by another ticket, they come from the session's AUTO_DEV_RESULT
    `review` block and its `review_round_<n>` friction lines, and the
    dev-queue row's disposition records (`cw review dispositions`) are listed
    on their own. Every degradation is named under `notes`; a data problem
    never fails the command.

    A ticket id is unique per client, not globally, and the event inbox is
    shared, so an event is used only when it can be tied to this row: a
    `stage.entered` event by one of the row's sessions, a
    `review.finding_voided` event whose finding is already in the row's
    settled records. The rest are counted under `notes`, never shown.

    Without -c, fails if the ticket is queued under more than one client.
    """
    store = load_dev_queue()
    resolved = resolve_view_client(store, ticket_id, client)
    task = _find_ticket(store, ticket_id, resolved, warn=False)
    inputs = collect_history_inputs(
        task,
        _client_config(resolved),
        _state_or_none(),
        worktree_override=worktree,
    )
    history = build_review_history(inputs)
    if output_json:
        click.echo(history.model_dump_json())
        return
    click.echo(render_review_history(history))
