"""``cw review dispositions`` — read the ledger currently bound to a ticket.

ADR-0016 named the hole this fills: the disposition ledger suppresses findings
durably and invisibly, and before #2232 there was no way to ask what it
currently holds for a ticket. ``cw event tail`` answers "what happened"; it
reads raw events, not the resolved per-ticket record, so it cannot answer
"what is suppressing right now" — which is the question an operator has to be
able to answer before arming the fuzzy claim tier.

Read-only and lock-free, modelled on ``cw dev-queue tasks``: ``load_dev_queue``
with no ``dev_queue_lock``, ``--client``/``--json`` filters, a human table plus
a JSON dump of the same data. Nothing here writes; rollback is
``cw review settle`` with ``outcome: REVERSED`` (#2232), which goes through the
ledger's one existing write chokepoint.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.config import load_effective_config
from cw.dev_queue import load_dev_queue
from cw.dev_queue.crud import _find_ticket, resolve_client
from cw.review_finding_dispositions import (
    disposition_drifted,
    split_disposition_key,
)

from ._group import review

if TYPE_CHECKING:
    from cw.review_finding_dispositions import FindingDisposition

#: Rendered in the STALE column when no ``--worktree`` was given. Drift is a
#: question about two commits in a repository, and without one there is no
#: repository to ask — which is not the same answer as "not stale", so it gets
#: its own cell value rather than borrowing "no".
_UNKNOWN = "?"

_HEADERS = ["FILE", "SUMMARY", "OUTCOME", "ACTOR", "RECORDED_AT", "SHA", "STALE"]
_COL_WIDTHS = [30, 44, 8, 16, 20, 10, 5]


def _head_sha(worktree: Path) -> str:
    """The worktree's current HEAD, or ``""`` when it cannot be resolved.

    Blank rather than raising: this command's job is to show the operator what
    the ledger holds, and a repository it cannot read the head of must not
    cost them that. :func:`disposition_drifted` reads a blank sha as "no
    question to answer", so the STALE column degrades to ``?``.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _stale_cell(
    worktree: Path | None, head_sha: str, entry: FindingDisposition, file: str
) -> str:
    """The STALE column for one record.

    Calls :func:`disposition_drifted` DIRECTLY, never through
    ``suppress_adjudicated_findings``' ``disposition_drift_check_enabled``
    gate. That gate scopes the automatic check on the shared suppression path;
    an operator who turned it off to debug is exactly the operator who most
    needs this diagnostic to keep answering (#2232).
    """
    if worktree is None or not head_sha:
        return _UNKNOWN
    return (
        "yes"
        if disposition_drifted(worktree, entry.reviewed_sha, head_sha, file)
        else "no"
    )


def _print_human(rows: list[tuple[str, ...]]) -> None:
    header = "  ".join(f"{h:<{w}}" for h, w in zip(_HEADERS, _COL_WIDTHS, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            "  ".join(f"{v:<{w}}" for v, w in zip(row, _COL_WIDTHS, strict=True))
        )


@review.command(name="dispositions")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client the ticket belongs to.")
@click.option(
    "--worktree",
    default=None,
    type=click.Path(path_type=Path, file_okay=False),
    help=(
        "A checkout to measure staleness against. Each record is compared at "
        "its own reviewed sha versus this worktree's HEAD; without it the "
        "STALE column reads '?'."
    ),
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON array.")
@handle_errors
def review_dispositions(
    ticket_id: str,
    client: str | None,
    worktree: Path | None,
    output_json: bool,
) -> None:
    """Show the disposition ledger currently bound to TICKET_ID (#2232).

    The "what is suppressing right now" view ADR-0016 called a precondition
    for ever arming the fuzzy claim tier. Read this before arming a lane, and
    when a finding you settled comes back and you want to know why.

    Every record is listed, whatever its outcome, with an explicit OUTCOME
    column. A `REVERSED` record is never filtered out: hiding a withdrawal
    entirely is a worse failure than showing one clearly labelled, and an
    operator must be able to tell a withdrawn settle from a live suppression
    at a glance. Only `REJECTED` actually suppresses; `ACCEPTED` is a
    record-only annotation and `REVERSED` is a withdrawal.

    With --worktree, STALE says whether the file changed between the commit
    each record was settled against and that worktree's HEAD — the same check
    the review pass itself now runs (#2232). A stale record is NOT expired:
    it still applies on any pass where its file has not moved. Re-settle it
    against the current code with `cw review settle`, or withdraw it with
    `outcome: REVERSED`.

    This reads the dev-queue row's last-synced copy of the ledger, which is
    updated after each review pass — NOT a live fetch of the ticket thread. A
    settle posted since the last pass will not appear here until the next one
    runs. Same snapshot-of-the-row convention as `cw dev-queue tasks`.
    """
    config = load_effective_config()
    resolved_client = resolve_client(ticket_id, config, client)
    task = _find_ticket(load_dev_queue(), ticket_id, resolved_client)

    head_sha = "" if worktree is None else _head_sha(worktree)
    entries = sorted(task.finding_dispositions.items())

    if output_json:
        click.echo(
            json.dumps(
                [
                    {
                        **entry.model_dump(mode="json"),
                        "key": key,
                        "file": split_disposition_key(key)[0],
                        "stale": _stale_cell(
                            worktree, head_sha, entry, split_disposition_key(key)[0]
                        ),
                    }
                    for key, entry in entries
                ]
            )
        )
        return

    if not entries:
        click.echo(f"No disposition records for ticket '{ticket_id}'.")
        return

    rows: list[tuple[str, ...]] = []
    for key, entry in entries:
        file, summary = split_disposition_key(key)
        rows.append(
            (
                file[: _COL_WIDTHS[0]],
                summary[: _COL_WIDTHS[1]],
                entry.outcome[: _COL_WIDTHS[2]],
                (entry.actor or "—")[: _COL_WIDTHS[3]],
                (entry.recorded_at or "—")[: _COL_WIDTHS[4]],
                (entry.reviewed_sha or "—")[: _COL_WIDTHS[5]],
                _stale_cell(worktree, head_sha, entry, file)[: _COL_WIDTHS[6]],
            )
        )
    _print_human(rows)
