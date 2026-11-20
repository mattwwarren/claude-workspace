"""The courier refresh turn: one read-only codex turn for a ticket snapshot (#3760).

RFC 0019 T2b, D-D2. On a tracker cw has no client for (an MCP-bound ticket), a
stage refreshes its snapshot with one ``codex exec`` turn whose only callable
tools are the binding's ``get`` and ``list_comments`` tools. The turn's result
is the bare T1 stdin document (:class:`~cw.tracker_io._prompt.RefreshDocument`),
parsed strictly and then handed to
:func:`~cw.ticket_snapshot._fetch.snapshot_from_stdin`, so cw computes
``agent_authored`` itself and the model never reports a status, a detail or a
ticket-id echo. A failure is a transport or parse failure and maps onto the
closed :data:`~cw.ticket_snapshot._refresh.RefreshReason` set; no reason is
added here.

The turn runs at stage entry, never on the dispatch tick or in ``reconcile()``
(D-B7; pinned by ``tests/test_refresh_never_on_tick.py``). It is read-only by
prompt and allowlist: under ``per_server`` (a plugin app) the whole app is
enabled and nothing detects a write by the turn, which is the residual exposure
ADR-0023 records. It is never retried, files no gate record and touches no
outbox row: a failure degrades to ``ticket.snapshot_degraded`` and the stage
runs on the last snapshot.

Exception contract: :func:`courier_snapshot` raises only :class:`RefreshError`.
It catches the narrow set ``(CwError, OSError, ValueError,
subprocess.SubprocessError)`` (every pydantic and config error is a
``ValueError``) and maps it to ``turn_failed``; any other exception is a
programming error and propagates.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from cw.exceptions import CwError
from cw.models.capabilities import ToolNarrowing
from cw.ticket_snapshot._fetch import snapshot_from_stdin
from cw.ticket_snapshot._models import SnapshotRefusedError
from cw.ticket_snapshot._refresh import RefreshError
from cw.tracker_io._argv import TurnKind, turn_tools
from cw.tracker_io._prompt import (
    REFRESH_SCHEMA,
    parse_refresh_document,
    render_refresh_prompt,
)
from cw.tracker_io._turn import courier_preflight, launch_turn, transport_refusal
from cw.tracker_io._verify import TrackerIoReason

if TYPE_CHECKING:
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot._models import TicketSnapshot
    from cw.ticket_snapshot._refresh import CourierFetch, RefreshReason
    from cw.tracker_io._turn import CourierTarget

_log = logging.getLogger(__name__)

#: Every refusal the preflight, the launch or the transport check can report,
#: onto the closed refresh vocabulary. An unlisted reason is ``turn_failed``.
REFRESH_REASON_MAP: dict[TrackerIoReason, RefreshReason] = {
    TrackerIoReason.TRACKER_UNSUPPORTED: "no_binding",
    TrackerIoReason.MCP_SERVER_UNNAMED: "courier_unreachable",
    TrackerIoReason.TRACKER_MCP_NOT_ENABLED: "courier_unreachable",
    TrackerIoReason.SCRATCH_UNWRITABLE: "courier_unreachable",
    TrackerIoReason.CODEX_UNAVAILABLE: "courier_unreachable",
    TrackerIoReason.PLAN_REFUSED: "refused",
    TrackerIoReason.TURN_TIMEOUT: "turn_failed",
    TrackerIoReason.TURN_EXIT_NONZERO: "turn_failed",
}


def _fail(reason: TrackerIoReason) -> RefreshError:
    """A :class:`RefreshError` for *reason*; the message carries no ticket text."""
    mapped = REFRESH_REASON_MAP.get(reason, "turn_failed")
    msg = f"courier refresh turn refused ({reason.value})"
    return RefreshError(msg, reason=mapped)


def _target(task: TicketTask, client: ClientConfig) -> CourierTarget:
    """The courier target for *task*'s ticket, or the refusal as a RefreshError."""
    tickets = client.bindings.tickets
    tracker = str(tickets.system) if tickets is not None else ""
    target = courier_preflight(task.ticket_id, tracker, client)
    if isinstance(target, TrackerIoReason):
        raise _fail(target)
    return target


def _launch(
    runner: CodexRunner, target: CourierTarget, ticket_id: str
) -> CodexRunResult:
    """Run the one refresh turn; a refusal or a transport failure is a RefreshError.

    ``write_capable=True`` makes an unreadable config layer refuse the launch
    (fail closed): the turn's output feeds trust-bearing snapshot text, so other
    servers must be known to be disabled.
    """
    launch = launch_turn(
        runner,
        schema=REFRESH_SCHEMA,
        prompt=render_refresh_prompt(
            ticket_id,
            server=target.server,
            tools=(target.get_tool, target.tools[1]),
            app=target.narrowing is not ToolNarrowing.PER_TOOL,
        ),
        server=target.server,
        allowlist=turn_tools(TurnKind.REFRESH, target.tools, get_tool=target.get_tool),
        write_capable=True,
        parts_total=0,
        narrowing=target.narrowing,
    )
    if launch.run is None:
        refusal = launch.refusal
        reason = refusal.reason if refusal is not None else None
        raise _fail(reason or TrackerIoReason.CODEX_UNAVAILABLE)
    transport = transport_refusal(launch.run, 0)
    if transport is not None:
        raise _fail(transport.reason or TrackerIoReason.TURN_EXIT_NONZERO)
    return launch.run


def _fetch(
    runner: CodexRunner, task: TicketTask, client: ClientConfig
) -> TicketSnapshot:
    """Preflight, launch, parse, and map the document to a snapshot."""
    target = _target(task, client)
    run = _launch(runner, target, task.ticket_id)
    document = parse_refresh_document(run.output_file_content)
    if document is None:
        msg = "courier refresh turn produced no valid snapshot document"
        raise RefreshError(msg, reason="turn_failed")
    try:
        return snapshot_from_stdin(
            document.model_dump_json(), task.ticket_id, task.client
        )
    except SnapshotRefusedError as exc:
        msg = "the courier refresh document was refused by the snapshot parser"
        raise RefreshError(msg, reason="refused") from exc


def courier_snapshot(
    runner: CodexRunner, task: TicketTask, client: ClientConfig
) -> TicketSnapshot:
    """Refresh *task*'s ticket through one read-only codex turn.

    Returns the snapshot (not yet recorded). Raises :class:`RefreshError` for
    every operational failure; see the module docstring for the contract.
    """
    try:
        return _fetch(runner, task, client)
    except RefreshError:
        raise
    except (CwError, OSError, ValueError, subprocess.SubprocessError) as exc:
        name = type(exc).__name__
        _log.warning("courier refresh failed type=%s", name)
        msg = f"courier refresh turn failed ({name})"
        raise RefreshError(msg, reason="turn_failed") from exc


def courier_refresher(runner: CodexRunner) -> CourierFetch:
    """A :data:`CourierFetch` that refreshes through *runner*'s codex turns."""

    def _courier(task: TicketTask, client: ClientConfig) -> TicketSnapshot:
        return courier_snapshot(runner, task, client)

    return _courier
