"""The ``cw outbox`` command group (#3705, RFC 0019 O1).

``cw outbox drain`` (#3706, RFC 0019 O2) lives in :mod:`cw.cli.outbox_drain`
and registers onto this group; the lazy map imports it alongside this module.

``cw outbox list`` is a pure read of the per-client outbox: it never reads a
body, never recovers a claim and never writes. A claimed entry is annotated
with the claimant verdict so an operator can tell a live claim from an orphaned
one.
"""

from __future__ import annotations

import click

from cw.cli._base import handle_errors, main, print_fixed_width_table
from cw.launch_marker import LaunchOwnerState
from cw.outbox import (
    VERDICT_IN_FLIGHT,
    VERDICT_OWNER_GONE,
    VERDICT_UNKNOWN,
    ClaimantVerdict,
    OutboxEntry,
    OutboxListEnvelope,
    OutboxListRow,
    OutboxState,
    claimant_state,
    list_clients,
    list_entries,
)

LIST_LIMIT_DEFAULT = 50
# Also the static ``short_help`` in ``cw.cli._lazy`` (the two must agree).
OUTBOX_GROUP_HELP = "Inspect and drain the per-client outbox of prose posts."

_NO_ENTRIES = "No outbox entries."
_HEADERS = ("id", "client", "state", "reason", "ticket", "producer", "created")
# id (32) | client | state, incl. "claimed (owner_gone)" | reason | ticket |
# producer | created
_COL_WIDTHS = (32, 16, 22, 22, 16, 18, 20)
_CREATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CELL_ELLIPSIS = "~"
_VERDICT_NAMES: dict[LaunchOwnerState, ClaimantVerdict] = {
    LaunchOwnerState.IN_FLIGHT: VERDICT_IN_FLIGHT,
    LaunchOwnerState.OWNER_GONE: VERDICT_OWNER_GONE,
    LaunchOwnerState.UNKNOWN: VERDICT_UNKNOWN,
}


def _cell(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - len(_CELL_ELLIPSIS)] + _CELL_ELLIPSIS


def _row(entry: OutboxEntry) -> OutboxListRow:
    verdict = claimant_state(entry)
    return OutboxListRow(
        id=entry.id,
        client=entry.client,
        role=entry.role,
        ticket=entry.ticket,
        marker=entry.marker,
        producer=entry.producer,
        state=entry.state,
        reason=entry.reason,
        detail=entry.detail,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        claimed_at=entry.claimed_at,
        claimant_verdict=None if verdict is None else _VERDICT_NAMES[verdict],
        part_count=len(entry.parts),
        body_bytes=entry.body_bytes,
    )


def collect_envelope(
    client: str | None, state: OutboxState | None, limit: int
) -> OutboxListEnvelope:
    """The merged, globally limited listing of the selected clients.

    ``list_entries`` is asked for ``limit + 1`` per client: the global top
    ``limit + 1`` is contained in the union of the per-client ones, so the
    ``truncated`` flag needs no separate count query. The state filter is
    applied before the limit.
    """
    clients = [client] if client is not None else list_clients()
    merged: list[OutboxEntry] = []
    for name in clients:
        merged.extend(list_entries(name, state=state, limit=limit + 1))
    merged.sort(key=lambda e: (e.created_at, e.id, e.client), reverse=True)
    return OutboxListEnvelope(
        entries=[_row(e) for e in merged[:limit]],
        limit=limit,
        truncated=len(merged) > limit,
    )


def _state_cell(row: OutboxListRow) -> str:
    if row.claimant_verdict is None:
        return row.state.value
    return f"{row.state.value} ({row.claimant_verdict})"


def _print_table(envelope: OutboxListEnvelope) -> None:
    rows = [
        (
            row.id,
            row.client,
            _state_cell(row),
            row.reason.value if row.reason else "-",
            row.ticket,
            row.producer,
            row.created_at.strftime(_CREATED_FORMAT),
        )
        for row in envelope.entries
    ]
    print_fixed_width_table(
        _HEADERS,
        _COL_WIDTHS,
        [[_cell(v, w) for v, w in zip(r, _COL_WIDTHS, strict=True)] for r in rows],
    )


@main.group("outbox", help=OUTBOX_GROUP_HELP)
def outbox_group() -> None:
    """Inspect the outbox store (``state_dir()/outbox/<client>``)."""


@outbox_group.command(
    "list", help="List outbox entries, newest first, without reading bodies."
)
@click.option(
    "-c",
    "--client",
    default=None,
    help=(
        "Limit to one client's outbox (default: every client directory present"
        " on disk)."
    ),
)
@click.option(
    "--state",
    "state_name",
    type=click.Choice([s.value for s in OutboxState]),
    default=None,
    help="Show only entries in this state.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=LIST_LIMIT_DEFAULT,
    help=f"Maximum entries to show across all clients (default: {LIST_LIMIT_DEFAULT}).",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Output the OutboxListEnvelope as JSON."
)
@handle_errors
def outbox_list(
    client: str | None, state_name: str | None, limit: int, as_json: bool
) -> None:
    """List entries newest first across every client (or one with ``-c``).

    Read-only: bodies are never read and no claim is recovered. ``--limit`` is
    global and applies after ``--state``.
    """
    state = None if state_name is None else OutboxState(state_name)
    envelope = collect_envelope(client, state, limit)
    if as_json:
        click.echo(envelope.model_dump_json())
    elif not envelope.entries:
        click.echo(_NO_ENTRIES)
    else:
        _print_table(envelope)
