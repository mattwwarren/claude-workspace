"""``cw session prune`` — retention command for the hot sessions.json (#1983).

Attaches to the existing ``session`` group defined in
:mod:`cw.cli.session_inspect`. Structural sibling of ``cw event prune`` in
:mod:`cw.cli.queues`; the divergences from that command are documented in the
command's own help text.
"""

from __future__ import annotations

import click

from cw.cli._base import handle_errors, parse_iso_before
from cw.cli.session_inspect import session_group
from cw.session_retention import prune_sessions


@session_group.command(name="prune")
@click.option(
    "--before",
    default=None,
    help=(
        "ISO 8601 timestamp; archive terminal sessions completed/started "
        "before this. Defaults to now - _SESSION_RETENTION_DAYS days "
        "if omitted."
    ),
)
@click.option(
    "--delete",
    "delete_flag",
    is_flag=True,
    help="Discard pruned sessions instead of archiving them.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Output the SessionPruneResult as JSON."
)
@handle_errors
def session_prune(before: str | None, delete_flag: bool, as_json: bool) -> None:
    """Archive terminal (completed/timed_out) sessions out of sessions.json.

    Unlike `cw event prune`, --before is optional and not part of a
    mutual-XOR pair with a --keep option: this command has no --keep
    (count-based retention has no stated use case for sessions).
    Omitted --before falls back to now - timedelta(days=_SESSION_RETENTION_DAYS)
    rather than erroring.

    A terminal session is skipped regardless of age if its
    (client, ticket_id) still has a live row in the dev-queue — see
    session_retention.prune_sessions for why. By default, pruned
    sessions are archived to sessions.<date>.json before being dropped
    from sessions.json; pass --delete to discard them instead.
    """
    before_ts = parse_iso_before(before) if before is not None else None
    result = prune_sessions(before=before_ts, archive=not delete_flag)

    if as_json:
        click.echo(result.model_dump_json())
    else:
        detail = f" (archive: {result.archive_path})" if result.archive_path else ""
        click.echo(
            f"Archived {result.archived_count}, deleted {result.deleted_count},"
            f" kept {result.kept_count}{detail}"
        )
