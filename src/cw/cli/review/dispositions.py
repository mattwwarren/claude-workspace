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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw._git import capture_head_sha
from cw.cli._base import handle_errors, print_fixed_width_table
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

#: Rendered in a cell whose question cannot be answered — the STALE column
#: with no ``--worktree`` or an unreadable one, and the AGE column for a
#: record whose ``recorded_at`` is blank or unparseable. Drift is a question
#: about two commits in a repository, and without one there is no repository
#: to ask — which is not the same answer as "not stale", so it gets its own
#: cell value rather than borrowing "no".
_UNKNOWN = "?"
#: Rendered for a field that is simply empty — no actor, no rationale. Distinct
#: from ``_UNKNOWN``: "nobody wrote one" is an answer, "cannot tell" is not.
_EMPTY = "—"

_HEADERS = [
    "FILE",
    "SUMMARY",
    "OUTCOME",
    "ACTOR",
    "REASON",
    "RECORDED_AT",
    "AGE",
    "SHA",
    "STALE",
]
_COL_WIDTHS = [30, 36, 8, 16, 24, 20, 5, 10, 5]

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _head_sha(worktree: Path) -> str:
    """The worktree's current HEAD, or ``""`` when it cannot be resolved.

    Blank rather than raising: this command's job is to show the operator what
    the ledger holds, and a repository it cannot read the head of must not
    cost them that. :func:`disposition_drifted` reads a blank sha as "no
    question to answer", so the STALE column degrades to ``?``.

    Delegates to the shared :func:`cw._git.capture_head_sha` (#2232), which
    strips inherited ``GIT_*`` variables. Without that, a ``cw`` process
    running inside a git hook resolves the HOOK's repository rather than the
    ``--worktree`` the operator named — silently answering the drift question
    about the wrong tree.
    """
    return capture_head_sha(worktree, strict=False)


def _age_cell(recorded_at: str) -> str:
    """Compact age (``Nm``/``Nh``/``Nd``) since *recorded_at*, or ``?``.

    Defensive by contract. ``FindingDisposition.recorded_at`` is a plain
    ``str`` that may be blank (a pre-#2210 record, or a producer with no clock
    handy) or unparseable (it arrives from hand-authored marker JSON), and a
    diagnostic surface must not lose the whole listing to one malformed field
    — so every failure renders ``?`` rather than raising. A naive timestamp is
    read as UTC, which is what ``cw review settle`` stamps; a record somehow
    dated in the future renders ``0m`` rather than a negative age.
    """
    if not recorded_at:
        return _UNKNOWN
    try:
        stamped = datetime.fromisoformat(recorded_at)
    except ValueError:
        return _UNKNOWN
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    seconds = max((datetime.now(UTC) - stamped).total_seconds(), 0.0)
    if seconds < _SECONDS_PER_HOUR:
        return f"{int(seconds // _SECONDS_PER_MINUTE)}m"
    if seconds < _SECONDS_PER_DAY:
        return f"{int(seconds // _SECONDS_PER_HOUR)}h"
    return f"{int(seconds // _SECONDS_PER_DAY)}d"


def _stale_cell(
    worktree: Path | None, head_sha: str, entry: FindingDisposition, file: str
) -> str:
    """The STALE column for one record.

    Calls :func:`disposition_drifted` DIRECTLY, never through
    ``suppress_adjudicated_findings``' ``disposition_drift_check_enabled``
    gate. That gate scopes the automatic check on the shared suppression path;
    an operator who turned it off to debug is exactly the operator who most
    needs this diagnostic to keep answering (#2232).

    A record carrying NO ``reviewed_sha`` renders ``?``, not ``no``.
    :func:`disposition_drifted` returns ``False`` for a blank stored sha
    because a missing field must not manufacture drift on the suppression
    path — but "there is nothing to compare against" is not "verified
    unchanged", and on a surface whose whole job is surfacing staleness,
    reporting unknown as clean is the wrong direction to fail.
    """
    if worktree is None or not head_sha or not entry.reviewed_sha:
        return _UNKNOWN
    return (
        "yes"
        if disposition_drifted(worktree, entry.reviewed_sha, head_sha, file)
        else "no"
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
    `outcome: REVERSED`. STALE reads '?' whenever the question cannot be
    answered — no --worktree, an unreadable one, or a record carrying no
    reviewed sha to compare against.

    REASON is the settling operator's own words and AGE is how long ago the
    record was stamped; both answer "is this suppression still the decision
    someone meant to make". A record whose `recorded_at` is blank or
    unparseable renders AGE as '?' rather than failing the listing.

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
                (entry.actor or _EMPTY)[: _COL_WIDTHS[3]],
                (entry.rationale or _EMPTY)[: _COL_WIDTHS[4]],
                (entry.recorded_at or _EMPTY)[: _COL_WIDTHS[5]],
                _age_cell(entry.recorded_at)[: _COL_WIDTHS[6]],
                (entry.reviewed_sha or _EMPTY)[: _COL_WIDTHS[7]],
                _stale_cell(worktree, head_sha, entry, file)[: _COL_WIDTHS[8]],
            )
        )
    print_fixed_width_table(_HEADERS, _COL_WIDTHS, rows)
