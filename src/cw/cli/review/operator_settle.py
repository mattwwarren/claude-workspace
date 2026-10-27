"""``cw review converge operator-settle`` -- false-positive ingress (#2787).

A clean-mode review that stopped on oscillation (a fixed finding came back) or on
an ``operator_action`` checklist is released without a commit only once every
recurring finding is settled. ``operator-settle`` is how the operator, on the
machine that dispatches, records "this recurring finding is a false positive" in
the ledger's settled memory. It reuses ``cw review settle``'s audit contract
(refuse inside a worker, a gh identity, a reason, an audit event before the
write) but keys on the ledger's finding identity, which is defined for a finding
with no diff anchor (``file: "N/A"``) where ``cw review settle``'s ledger
builder is not.

Steps, in the order that matters:

1. Refuse inside a headless worker, refuse a blank ``--reason``, resolve the gh
   identity and stamp the time.
2. Resolve the dev-queue row; refuse while a worker owns it (``RUNNING``); load
   the ledger and refuse one that belongs to another ticket.
3. Match every entry against the identities the ledger has recorded. Any
   unmatched entry (a mis-copied anchor, #2319) prints the full JSON, exits 1
   and writes nothing, even when other entries matched.
4. Emit one ``review.finding_settled`` event per settled identity, THEN save the
   ledger: an emit failure aborts before the write.
5. Look the row up again. If a worker started in between, one of two ledger
   writes may have been lost, so print a warning and exit 1; re-running is
   idempotent.
6. Print ``{ledger, settled, unmatched, releasable, next}`` and exit 0.

It writes nothing to the dev-queue row.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from pydantic import BaseModel, ConfigDict, Field

from cw.cli._base import handle_errors
from cw.cli._worker_guard import refuse_inside_dispatch_worker
from cw.cli.review._group import _parse_payload_or_exit
from cw.cli.review.commands import _resolve_settle_actor, _utc_now_iso
from cw.cli.review.converge import (
    converge,
    emit_json,
    require_ledger,
)
from cw.config import load_effective_config
from cw.dev_queue import load_dev_queue
from cw.dev_queue.crud import _find_ticket, resolve_client
from cw.exceptions import CwError
from cw.models import QueueItemStatus
from cw.models.enums import OrchestratorEventType
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    SettleTarget,
    operator_settle,
    operator_settle_event_payload,
    release_cause,
    save_ledger,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import TicketTask
    from cw.review_convergence import ConvergenceLedger, SettledEntry

_SETTLE_IN_WORKER_MSG = (
    "Refusing to settle a review finding from inside a dispatch worker "
    "(.claude/cw-context.json reports headless). A ledger entry silently "
    "suppresses every future re-raise of that finding, so a worker must not "
    "mint one for its own reviewer. Hand the payload to the orchestrator "
    "session, which runs `cw review converge operator-settle` outside the worker."
)
_UNMATCHED_HINT = (
    "no ledger entry was written. Each entry must match an identity the ledger "
    "already recorded (stop_identities, fixed or settled): copy severity, file "
    "and summary verbatim from `cw review converge status` stop.settle_payload, "
    "or from `nearest` below."
)
_RACE_WARNING = "row_started_running"
_RACE_MESSAGE = (
    "a worker started on this ticket while the settle was being written, so one "
    "of the two ledger writes may have been lost. Run `cw review converge "
    "status` once the ticket parks again and re-run the same operator-settle "
    "(idempotent: one entry per identity, the newest settled_at wins)."
)


class _OperatorSettleInput(BaseModel):
    """The ``operator-settle`` request: the findings to settle, verbatim."""

    model_config = ConfigDict(extra="forbid")

    entries: list[SettleTarget] = Field(min_length=1)


def _row_ledger_path(row: TicketTask) -> Path:
    """The ledger inside the parked row's worktree, or a ``CwError``."""
    if row.worktree_path is None:
        msg = (
            f"dev-queue row for {row.ticket_id} has no worktree_path, so its "
            "ledger cannot be located; pass --ledger <path>"
        )
        raise CwError(msg)
    return row.worktree_path / LEDGER_RELATIVE_PATH


def _require_parked(row: TicketTask) -> None:
    if row.status is QueueItemStatus.RUNNING:
        msg = (
            f"{row.ticket_id} is RUNNING: a live worker owns the convergence "
            "ledger. Settle after the ticket parks."
        )
        raise CwError(msg)


def _emit_operator_settle_events(
    entries: Sequence[SettledEntry], ticket_id: str
) -> None:
    """One ``review.finding_settled`` audit event per settled identity.

    Raises rather than degrading, as ``cw review settle`` does: the caller must
    not write the ledger for a finding whose audit did not record, so an emit
    failure aborts the whole settle, before any write.
    """
    from cw.events import record_event

    for entry in entries:
        try:
            record_event(
                OrchestratorEventType.REVIEW_FINDING_SETTLED,
                payload=operator_settle_event_payload(entry),
                correlation_id=ticket_id,
            )
        except OSError as exc:
            msg = (
                "Could not record the review.finding_settled audit event for "
                f"{entry.identity.file} ({entry.identity.summary_normalized!r}): "
                f"{exc}. Nothing was written. A suppression with no audit "
                "record is invisible, so the settle is refused rather than "
                "recorded half-way. Fix the event store and re-run the same "
                "payload."
            )
            raise CwError(msg) from exc


def _load_owned_ledger(path: Path, ticket_id: str) -> ConvergenceLedger:
    """The ledger at *path*, refused when it belongs to another ticket (R21)."""
    ledger = require_ledger(path)
    if ledger.ticket_id != ticket_id:
        msg = (
            f"the ledger at {path} belongs to ticket {ledger.ticket_id}, not "
            f"{ticket_id}; refusing to settle against another ticket's review"
        )
        raise CwError(msg)
    return ledger


@converge.command(name="operator-settle")
@click.argument("path")
@click.option("--ticket-id", required=True, help="The parked ticket to settle for.")
@click.option("--client", "-c", default=None, help="Client the ticket belongs to.")
@click.option(
    "--reason",
    required=True,
    type=str,
    help="Why these findings are false positives. Required; recorded on every entry.",
)
@click.option(
    "--ledger",
    "ledger_override",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help=(
        "The ledger to settle. Default: .cw/review-convergence.json inside the "
        "dev-queue row's worktree."
    ),
)
@handle_errors
def converge_operator_settle(
    path: str,
    ticket_id: str,
    client: str | None,
    reason: str,
    ledger_override: Path | None,
) -> None:
    """Settle recurring review findings you judge false positives (#2787).

    PATH is a file or '-'. Payload: {"entries": [{"severity": "MUST_FIX" |
    "SHOULD_FIX", "file": "<path or N/A>", "summary": "<finding summary>"}]}.
    `converge status` (`stop.settle_payload`) and the convergence history
    comment print it ready to paste.

    Run it on the machine that dispatches, once the ticket has parked, then
    `cw dev-queue requeue <T> -c <C>`: an oscillation stop releases on the
    same tip once every recurring finding is settled, and an operator_action
    stop releases on the same tip regardless. Only an identity the ledger
    already recorded can be settled; an entry that matches nothing prints the
    full JSON with its nearest recorded identity, exits 1 and writes nothing
    (all or nothing). There is no flag that bypasses the match.

    The ledger is the row's worktree ledger unless --ledger names one; either
    way its ticket must be --ticket-id. Emits one `review.finding_settled`
    event per settled identity BEFORE writing the ledger. Refused inside a
    headless worker, with a blank --reason, with no gh identity, and while the
    row is RUNNING.
    """
    refuse_inside_dispatch_worker(_SETTLE_IN_WORKER_MSG)
    if not reason.strip():
        msg = (
            "--reason must not be blank: a suppression with no stated reason "
            "is not an audit record"
        )
        raise CwError(msg)
    actor = _resolve_settle_actor()
    now = _utc_now_iso()
    payload = _parse_payload_or_exit(path, _OperatorSettleInput)

    resolved_client = resolve_client(ticket_id, load_effective_config(), client)
    row = _find_ticket(load_dev_queue(), ticket_id, resolved_client)
    _require_parked(row)
    target_path = ledger_override or _row_ledger_path(row)
    ledger = _load_owned_ledger(target_path, ticket_id)

    try:
        updated, report = operator_settle(
            ledger, payload.entries, actor=actor, reason=reason, now=now
        )
    except ValueError as exc:
        raise CwError(str(exc)) from exc
    if report.unmatched:
        emit_json(
            {
                "ledger": str(target_path),
                "written": False,
                "settled": [],
                "unmatched": [u.model_dump(mode="json") for u in report.unmatched],
                "releasable": release_cause(ledger) is not None,
                "hint": _UNMATCHED_HINT,
            }
        )
        raise click.exceptions.Exit(1)

    _emit_operator_settle_events(report.settled, ticket_id)
    save_ledger(target_path, updated)
    result: dict[str, object] = {
        "ledger": str(target_path),
        "settled": [e.model_dump(mode="json") for e in report.settled],
        "unmatched": [],
        "releasable": report.releasable,
        "next": f"cw dev-queue requeue {ticket_id} -c {resolved_client}",
    }
    again = _find_ticket(load_dev_queue(), ticket_id, resolved_client)
    if again.status is QueueItemStatus.RUNNING:
        result["warning"] = _RACE_WARNING
        emit_json(result)
        raise CwError(_RACE_MESSAGE)
    emit_json(result)
