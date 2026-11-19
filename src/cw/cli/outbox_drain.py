"""``cw outbox drain`` (#3706, RFC 0019 O2).

Registers onto the ``cw outbox`` group; ``cw.cli._lazy`` imports this module
alongside :mod:`cw.cli.outbox` (``also_import``). The drain engine (and with
it the ``gh`` and tracker post code) is imported only when a drain runs, through
:func:`_engine`, so resolving ``cw outbox`` (or running ``cw outbox list``)
stays light. ``cw.config`` and ``cw.outbox`` are already loaded by
:mod:`cw.cli.outbox`.

Exit codes: ``0`` drained or nothing to do; ``1`` an internal error, an unknown
``-c`` client, or the refusal of a plain drain of a client with
``outbox.enabled: false``; ``2`` a usage error; ``os.EX_TEMPFAIL`` (75) another
drain holds a client's lease. With several clients the precedence is 1, 75, 0.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import click

from cw import config
from cw.cli._base import handle_errors
from cw.cli.outbox import OUTBOX_GROUP_HELP, outbox_group
from cw.exceptions import CwError
from cw.models.client import OUTBOX_MAX_ENTRIES_PER_RUN_CEILING
from cw.outbox import list_clients

if TYPE_CHECKING:
    from types import ModuleType

    from cw.models import ClientConfig
    from cw.outbox_drain import DrainReport

GROUP_HELP = OUTBOX_GROUP_HELP
DRAIN_HELP = "Drain pending outbox entries to the ticket tracker through gh."
CLIENT_HELP = (
    "Drain one client (default: every configured client that has an outbox directory)."
)
LIMIT_HELP = (
    "Maximum entries to claim per client this run (default: the client's"
    f" outbox.max_entries_per_run; 1 to {OUTBOX_MAX_ENTRIES_PER_RUN_CEILING})."
)
FORCE_HELP = (
    "Operator override: also drain a client with outbox.enabled: false, and"
    " first clear the held-entry list and every unreachable marker so those"
    " entries are retried."
)
JSON_HELP = "Output the drain envelope as JSON."

_NOTHING = "No outbox clients to drain."
_UNCONFIGURED = "unconfigured (no usable gh tickets binding): {names}"
_SKIPPED_DISABLED = "skipped (outbox.enabled is false): {names}"
_REFUSED = (
    "Client '{name}' has outbox.enabled set to false, so its outbox is not"
    " drained. Run 'cw outbox drain -c {name} --force' to drain it anyway."
)


def _engine() -> ModuleType:
    """:mod:`cw.outbox_drain`, imported on first use (see the module docstring).

    ``importlib`` rather than an in-function ``import`` statement, the same
    mechanism ``cw.cli._lazy`` uses to resolve commands.
    """
    return importlib.import_module("cw.outbox_drain")


@dataclass
class _Selection:
    """The clients to drain and the names reported on stderr."""

    clients: list[tuple[str, ClientConfig]] = field(default_factory=list)
    unconfigured: set[str] = field(default_factory=set)
    disabled: list[str] = field(default_factory=list)


def _select(name: str | None, *, force: bool) -> _Selection:
    select_target = _engine().select_target
    selection = _Selection()
    if name is not None:
        cfg = config.get_client(name)
        if not cfg.outbox.enabled and not force:
            raise click.ClickException(_REFUSED.format(name=name))
        candidates = [(name, cfg)]
    else:
        configured = config.load_clients()
        candidates = []
        for client in list_clients():
            cfg_or_none = configured.get(client)
            if cfg_or_none is None:
                selection.unconfigured.add(client)
            elif not cfg_or_none.outbox.enabled and not force:
                selection.disabled.append(client)
            else:
                candidates.append((client, cfg_or_none))
    for client, cfg in candidates:
        if select_target(cfg) is None:
            selection.unconfigured.add(client)
        selection.clients.append((client, cfg))
    return selection


def _text_line(report: DrainReport) -> str:
    unreachable = ",".join(report.unreachable_roles) or "-"
    return (
        f"{report.client}: drained={report.drained} delivered={report.delivered}"
        f" unverified={report.unverified} skipped={report.skipped}"
        f" released={report.released} held={report.held}"
        f" lease_held={'yes' if report.lease_held else 'no'}"
        f" unreachable={unreachable}"
    )


def _print(reports: list[DrainReport], selection: _Selection, *, as_json: bool) -> None:
    unconfigured = sorted(selection.unconfigured)
    if as_json:
        envelope = _engine().DrainEnvelope(clients=reports, unconfigured=unconfigured)
        click.echo(envelope.model_dump_json())
    elif not reports:
        click.echo(_NOTHING)
    else:
        for report in reports:
            click.echo(_text_line(report))
    if unconfigured:
        click.echo(_UNCONFIGURED.format(names=", ".join(unconfigured)), err=True)
    if selection.disabled:
        names = ", ".join(selection.disabled)
        click.echo(_SKIPPED_DISABLED.format(names=names), err=True)


@outbox_group.command("drain", help=DRAIN_HELP)
@click.option("-c", "--client", default=None, help=CLIENT_HELP)
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=OUTBOX_MAX_ENTRIES_PER_RUN_CEILING),
    default=None,
    help=LIMIT_HELP,
)
@click.option("--force", is_flag=True, help=FORCE_HELP)
@click.option("--json", "as_json", is_flag=True, help=JSON_HELP)
@handle_errors
def outbox_drain(
    client: str | None, limit: int | None, force: bool, as_json: bool
) -> None:
    """Drain the selected clients' outboxes once each, then print the report."""
    engine = _engine()
    selection = _select(client, force=force)
    reports: list[DrainReport] = []
    failed = False
    for name, cfg in selection.clients:
        try:
            reports.append(engine.run_drain(name, cfg, limit=limit, force=force))
        except CwError as exc:
            # cw-authored message, the same text handle_errors would print.
            click.echo(f"Error: {name}: {exc}", err=True)
            failed = True
    _print(reports, selection, as_json=as_json)
    if failed:
        click.get_current_context().exit(1)
    if any(report.lease_held for report in reports):
        click.get_current_context().exit(os.EX_TEMPFAIL)
