"""The ``cw ticket`` command group (RFC 0019 T1, GitHub #3711).

``cw ticket snapshot`` writes a digest-keyed snapshot of a ticket to the store,
stamps the row's digest and renders ``.cw/context.json`` in the current
worktree. On a ``github-issues`` client cw fetches the issue itself; on any
other tracker (including an unset one, strictly by ``resolve_tracker``) it reads
one cw-defined stdin document (``--from-json -``), which is model-reported.

Exit codes: 0 ok, 1 refused (a ``CwError``: bad input, no fetchable ticket,
size cap), 2 usage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw._hook_context import _read_cw_context
from cw.cli._base import handle_errors, main
from cw.config import get_client
from cw.exceptions import CwError
from cw.ticket_snapshot import (
    TicketSnapshot,
    record_snapshot,
    render_context,
    snapshot_from_github,
    snapshot_from_stdin,
    write_context_json,
)
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from cw.models import ClientConfig

_ISSUE_NUMBER_RE = re.compile(r"#?\d+")
_CONTEXT_SESSION_KEY = "session_id"
_CONTEXT_CLIENT_KEY = "client"


@main.group("ticket", help="Snapshot a ticket into the digest-keyed store.")
def ticket_group() -> None:
    """Ticket snapshot commands (RFC 0019 T1)."""


def _resolve_client(
    client_name: str | None, context: dict[str, object] | None
) -> ClientConfig:
    """``-c``, else ``client`` from cw-context.json; a usage error otherwise."""
    name = client_name
    if name is None and context is not None:
        ctx_client = context.get(_CONTEXT_CLIENT_KEY)
        name = ctx_client if isinstance(ctx_client, str) and ctx_client else None
    if name is None:
        msg = "no client: pass -c/--client or run from a cw worktree"
        raise click.UsageError(msg)
    try:
        return get_client(name)
    except CwError as exc:
        raise click.UsageError(str(exc)) from exc


def _snapshot_for(
    ticket: str, client: ClientConfig, from_json: str | None
) -> TicketSnapshot:
    """Fetch from GitHub, or parse the stdin document, by the tracker of the client."""
    if resolve_tracker(client.workspace_path) == TRACKER_GITHUB_ISSUES:
        if from_json is not None:
            msg = (
                "--from-json is refused on a github-issues client: the command"
                " fetches the issue itself"
            )
            raise CwError(msg)
        if not _ISSUE_NUMBER_RE.fullmatch(ticket):
            msg = "TICKET must be an issue number on a github-issues client"
            raise click.UsageError(msg)
        return snapshot_from_github(ticket, client.name, client.workspace_path)
    if from_json is None:
        msg = "non-GitHub client: pass --from-json - with the ticket document on stdin"
        raise CwError(msg)
    text = click.get_text_stream("stdin").read()
    return snapshot_from_stdin(text, ticket, client.name)


@ticket_group.command(
    "snapshot",
    help="Snapshot TICKET into the store and render .cw/context.json here.",
)
@click.argument("ticket")
@click.option("-c", "--client", "client_name", default=None, help="Client name.")
@click.option(
    "--from-json",
    "from_json",
    type=click.Choice(["-"]),
    default=None,
    help="Read the ticket document from stdin (non-GitHub clients only).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option("--refresh", is_flag=True, help="Record as a refresh (same behavior).")
@handle_errors
def snapshot_cmd(
    ticket: str,
    client_name: str | None,
    from_json: str | None,
    as_json: bool,
    refresh: bool,
) -> None:
    """Snapshot TICKET, stamp the row's digest, and render the context file."""
    cwd = Path.cwd()
    context = _read_cw_context(str(cwd))
    client = _resolve_client(client_name, context)
    snapshot = _snapshot_for(ticket, client, from_json)
    record = record_snapshot(snapshot, refresh=refresh)
    session = context.get(_CONTEXT_SESSION_KEY) if context is not None else None
    rendered = render_context(
        snapshot,
        record.snapshot_digest,
        materialized_by_session=session if isinstance(session, str) else None,
    )
    context_path = write_context_json(cwd, rendered)
    if as_json:
        result = {
            "ticket_id": snapshot.ticket_id,
            "client": snapshot.client,
            "snapshot_digest": record.snapshot_digest,
            "snapshot_fetched_at": rendered["snapshot_fetched_at"],
            "previous_digest": record.previous_digest,
            "comments": record.comments,
            "row_updated": record.row_updated,
            "context_path": str(context_path),
        }
        click.echo(json.dumps(result, sort_keys=True))
        return
    click.echo(
        f"snapshot {record.snapshot_digest} ticket={snapshot.ticket_id}"
        f" comments={record.comments} row_updated={record.row_updated}"
        f" -> {context_path}"
    )
