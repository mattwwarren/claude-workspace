"""Dev-queue retention commands: clear and prune.

Split out of ``crud.py`` to keep it under the ~1000-line ceiling. See
``crud.py`` for add/move/requeue and ``approve.py`` for ``approve``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import click

from cw.cli._base import handle_errors
from cw.dev_queue import (
    DEFAULT_PRUNE_OLDER_THAN_DAYS,
    _prune_age_basis,
    clear_tickets,
    prune_tickets,
    select_clearable_tickets,
    select_prunable_tickets,
)
from cw.models import QueueItemStatus, TicketTask

from ._group import dev_queue


@dev_queue.command(name="clear")
@click.option("--client", "-c", "client", required=True, help="Client name")
@click.option(
    "--status",
    "-s",
    "status_filter",
    type=click.Choice([e.value for e in QueueItemStatus]),
    default=None,
    help=(
        "Optional status filter. Without it, RUNNING, BLOCKED_ON_USER, and"
        " AWAITING_OPERATOR_SIGNOFF rows are excluded from the sweep -- name"
        " one of them here explicitly to delete it."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be cleared without deleting anything. Never mutates state.",
)
@click.option(
    "--confirm",
    "confirm",
    is_flag=True,
    default=False,
    help=(
        "Actually delete the matched rows. Without --confirm (and without"
        " --dry-run) the command only previews; --dry-run always wins if"
        " both are given."
    ),
)
@handle_errors
def dev_queue_clear(
    client: str,
    status_filter: str | None,
    dry_run: bool,
    confirm: bool,
) -> None:
    """Delete dev-queue tasks for a client -- destructive; previews by default.

    Pass --confirm to actually remove the matched rows; --dry-run always
    previews and wins if both are given. With no --status, RUNNING,
    BLOCKED_ON_USER, and AWAITING_OPERATOR_SIGNOFF rows are excluded from
    the sweep -- naming one of them explicitly via --status targets it for
    deletion instead of skipping it. Takes the same dev-queue file lock the
    dispatch loop takes and computes the deleted set exactly once per
    invocation, so a --confirm run can never silently grow past what it
    reports (#2003).
    """
    status_enum = QueueItemStatus(status_filter) if status_filter else None

    if dry_run or not confirm:
        candidates = select_clearable_tickets(client, status=status_enum)
        _print_task_summary(candidates)
        if not candidates:
            click.echo("Nothing to clear.")
        elif dry_run:
            click.echo(
                f"{len(candidates)} task(s) would be cleared"
                " (dry-run; nothing was deleted)."
            )
        else:
            click.echo(
                f"{len(candidates)} task(s) would be cleared."
                " Pass --confirm to actually delete them."
            )
        return

    # Why clear_tickets alone, with no preceding select_clearable_tickets
    # call: the library derives the candidate set exactly once under the
    # dev-queue lock and deletes precisely that set, so rendering its return
    # value reports exactly what was removed. Previewing first would
    # re-derive under a second, later lock -- a TOCTOU window in which a
    # concurrent dispatch tick could grow the deleted set past what was
    # shown. Mirrors dev_queue_prune's identical precedent.
    # bounded=True (#2501): operator --confirm path; nothing precedes the lock.
    removed = clear_tickets(client, status=status_enum, bounded=True)
    _print_task_summary(removed)
    click.echo(f"Cleared {len(removed)} dev-queue task(s) for {client}.")


def _print_task_summary(tasks: list[TicketTask]) -> None:
    """Render TICKET_ID/CLIENT/STATUS/AGE_DAYS columns for *tasks*.

    Shared by `prune` (#382) and `clear` (#2003). Mirrors ``tasks.py``'s
    ``_print_tasks_human`` column-table convention; module-private to this
    file since that helper is itself module-private. AGE_DAYS uses
    ``_prune_age_basis`` -- the same age basis ``_select_prune_candidates``
    filters on -- so a row's displayed age cannot disagree with the reason
    it was selected for `prune`; for `clear` the column is informational
    only (clear does not filter on age).
    """
    if not tasks:
        return
    headers = ["TICKET_ID", "CLIENT", "STATUS", "AGE_DAYS"]
    col_widths = [12, 16, 26, 8]
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    now = datetime.now(UTC)
    for t in tasks:
        age_days = (now - _prune_age_basis(t)).days
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:26],
            str(age_days)[:8],
        ]
        click.echo("  ".join(f"{v:<{w}}" for v, w in zip(row, col_widths, strict=True)))


def _parse_prune_statuses(status_filter: str) -> frozenset[QueueItemStatus]:
    """Parse ``--status`` as a comma-separated status list (#382).

    Mirrors ``session_wait``'s ``--until`` precedent: a plain ``str`` option
    parsed into enum members, with ``ValueError`` converted to
    ``click.BadParameter`` -- not a ``click.Choice``, which cannot express a
    comma-separated multi-value.
    """
    try:
        statuses = frozenset(
            QueueItemStatus(s.strip()) for s in status_filter.split(",") if s.strip()
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--status") from exc
    if not statuses:
        msg = "must name at least one status"
        raise click.BadParameter(msg, param_hint="--status")
    return statuses


@dev_queue.command(name="prune")
@click.option(
    "--client",
    "-c",
    "client",
    default=None,
    help=(
        "Client to prune. Required unless --all-clients is given -- prune"
        " never scopes across every client by omission alone."
    ),
)
@click.option(
    "--all-clients",
    "all_clients",
    is_flag=True,
    default=False,
    help=(
        "Prune matching rows across every configured client in one"
        " invocation. The only sanctioned way to cross the tenant boundary"
        " with this command; incompatible with --status pending."
    ),
)
@click.option(
    "--older-than",
    "older_than_days",
    type=int,
    default=DEFAULT_PRUNE_OLDER_THAN_DAYS,
    show_default=True,
    help=(
        "Only prune rows whose completed_at (or created_at, for rows with"
        " no completed_at -- e.g. CANCELLED) is strictly older than this"
        " many days."
    ),
)
@click.option(
    "--status",
    "-s",
    "status_filter",
    default="completed",
    show_default=True,
    help=(
        "Comma-separated status(es) to prune: completed, failed, cancelled,"
        " or pending. `pending` is only ever prunable when named here"
        " explicitly together with a single --client -- never by default,"
        " never via --all-clients. running, blocked_on_user, and"
        " awaiting_operator_signoff are never eligible at any age."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be pruned without deleting anything. Never mutates state.",
)
@click.option(
    "--confirm",
    "confirm",
    is_flag=True,
    default=False,
    help=(
        "Actually delete the matched rows. Without --confirm (and without"
        " --dry-run) the command only previews; --dry-run always wins if"
        " both are given."
    ),
)
@handle_errors
def dev_queue_prune(
    client: str | None,
    all_clients: bool,
    older_than_days: int,
    status_filter: str,
    dry_run: bool,
    confirm: bool,
) -> None:
    """Delete stale terminal dev-queue rows past --older-than days.

    Defaults to COMPLETED rows only, 90 days old, and previews without
    deleting anything -- pass --confirm to actually remove the matched
    rows. RUNNING and BLOCKED_ON_USER rows are never touched at any age
    (live or operator-parked work); PENDING rows are prunable only when
    --status pending is named explicitly together with a single --client.
    Takes the same dev-queue file lock the dispatch loop takes and computes
    the deleted set exactly once per invocation, so a --confirm run can
    never silently grow past what it reports (#382).
    """
    # Why not click's own `required=True` (the `clear`/`drain` precedent):
    # --client and --all-clients are alternatives, so requiring the option
    # outright would forbid the escape hatch. This reproduces the same
    # guarantee -- you cannot silently omit the tenant boundary -- as a
    # UsageError raised before any library call runs.
    if client is None and not all_clients:
        msg = "Must pass either --client or --all-clients."
        raise click.UsageError(msg)
    if client is not None and all_clients:
        msg = "--client and --all-clients are mutually exclusive."
        raise click.UsageError(msg)

    statuses = _parse_prune_statuses(status_filter)

    if dry_run or not confirm:
        candidates = select_prunable_tickets(
            statuses, older_than_days, client, all_clients=all_clients
        )
        _print_task_summary(candidates)
        if not candidates:
            click.echo("Nothing to prune.")
        elif dry_run:
            click.echo(
                f"{len(candidates)} task(s) would be pruned"
                " (dry-run; nothing was deleted)."
            )
        else:
            click.echo(
                f"{len(candidates)} task(s) would be pruned."
                " Pass --confirm to actually delete them."
            )
        return

    # Why prune_tickets alone, with no preceding select_prunable_tickets call:
    # the library derives the candidate set exactly once under the dev-queue
    # lock and deletes precisely that set, so rendering its return value
    # reports exactly what was removed. Previewing first would re-derive under
    # a second, later lock -- a TOCTOU window in which a concurrent dispatch
    # tick could grow the deleted set past what was shown.
    # bounded=True (#2501): operator --confirm path; nothing precedes the lock.
    removed = prune_tickets(
        statuses, older_than_days, client, all_clients=all_clients, bounded=True
    )
    _print_task_summary(removed)
    click.echo(f"Pruned {len(removed)} dev-queue task(s).")
