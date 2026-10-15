"""``cw dev-queue show``: one ticket's full row plus its legal next commands (#2691).

Also home of :func:`echo_resulting_row`, which ``add``, ``move``, ``requeue``,
``approve`` and ``cancel`` call after their confirmation line so the caller
sees the row the mutation left behind. Both only read: no lock, no
subprocess, no event recorded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import click

from cw.cli._base import handle_errors
from cw.dev_queue import load_dev_queue
from cw.exceptions import CwError
from cw.ticket_view import (
    ViewSources,
    build_ticket_view,
    render_ticket_view,
    resolve_view_client,
)

from ._group import dev_queue

_log = logging.getLogger(__name__)


def echo_resulting_row(
    ticket_id: str, client: str, *, sources: ViewSources | None = None
) -> None:
    """Print a blank line, then the view of *ticket_id*'s row in *client*.

    Best-effort: the mutation that calls this has already succeeded and
    printed its confirmation, so a failure to build the view (the row is
    gone, a config or state file is unreadable) prints nothing and never
    changes the exit code. The swallow is logged at DEBUG, not WARNING: a
    WARNING would reach stderr through logging's last-resort handler and
    change the verbs' stderr. Pass one *sources* to every call of a
    multi-ticket command so the shared reads happen once.
    """
    now = datetime.now(UTC)
    try:
        view = build_ticket_view(
            ticket_id, client, now=now, sources=sources or ViewSources()
        )
    except (CwError, OSError, ValueError):
        _log.debug(
            "row echo skipped for %s/%s: the view could not be built",
            client,
            ticket_id,
            exc_info=True,
        )
        return
    click.echo("")
    click.echo(render_ticket_view(view, now=now))


@dev_queue.command(name="show")
@click.argument("ticket_id", metavar="TICKET")
@click.option(
    "--client",
    "-c",
    default=None,
    help="Client name. Default: the one client whose queue holds TICKET.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Emit the TicketView JSON (schema: `cw schema show ticket-view`).",
)
@handle_errors
def dev_queue_show(ticket_id: str, client: str | None, output_json: bool) -> None:
    """Show one ticket's dev-queue row and the commands that can act on it.

    Prints one `key: value` per line in a fixed key order, every key always
    present (`-` when absent), nothing truncated: status, stage, disposition,
    blocker reason and full details, lane, priority, attempts against the
    resolved attempt ceiling, the bound session and its daemon liveness, the
    last result, the stored PR state, why a PENDING row is not dispatching
    (with where that answer came from and how old it is), the approvals
    recorded on the row, and `next:` -- the commands that are legal for the
    row as it stands.

    With several rows for the same ticket and client, shows the one
    `cw dev-queue wait` and `requeue` bind to and reports the rest as
    `other_rows`. Without -c, fails if the ticket is queued under more than
    one client.
    """
    resolved = resolve_view_client(load_dev_queue(), ticket_id, client)
    now = datetime.now(UTC)
    view = build_ticket_view(ticket_id, resolved, now=now, sources=ViewSources())
    if output_json:
        click.echo(view.model_dump_json())
        return
    click.echo(render_ticket_view(view, now=now))
