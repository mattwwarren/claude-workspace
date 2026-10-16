"""What ``cw dev-queue requeue`` prints and records once the requeue ran (#2692).

Split out of ``crud`` to keep that module under the ~1000-line ceiling:
the idempotent PENDING no-op echo (with its read-only live-session notes),
the inferred-``--regress`` announcement, the ``--reset-attempts`` confirmation
(#2695), and the ``TICKET_REQUEUED`` event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.events import record_event
from cw.models import OrchestratorEventType, Stage

from ._settle import live_session_notes
from .show import echo_resulting_row

if TYPE_CHECKING:
    from cw.native_daemon import NativeDaemonClient

# The evidence a regress into each stage clears (lifecycle._stage_regress).
_REGRESS_EVIDENCE: dict[str, str] = {
    Stage.PLAN.value: (
        "  Regressing into plan cleared the plan approval (plan_approved_at,"
        " plan_approved_fingerprint); approve the new plan draft again."
    ),
    Stage.REVIEW.value: (
        "  Regressing into review cleared any recorded MUST_FIX override;"
        " record a new one against the fresh verdict if you still need it."
    ),
}


def echo_pending_noop(
    ticket_id: str,
    client: str,
    stage: str,
    daemon: NativeDaemonClient,
    *,
    reset_attempts_ignored: bool = False,
) -> None:
    """The idempotent no-op: say so, name any live session, echo the row."""
    click.echo(
        f"Ticket {ticket_id} ({client}) is already PENDING at {stage};"
        " nothing to requeue."
    )
    if reset_attempts_ignored:
        click.echo(
            "note: --reset-attempts was not applied; the row is already"
            " PENDING and nothing was written.",
            err=True,
        )
    for note in live_session_notes(ticket_id, client, daemon):
        click.echo(note, err=True)
    echo_resulting_row(ticket_id, client)


def _requeue_reason(result: dict[str, str | bool | int]) -> str:
    """The ``TICKET_REQUEUED`` reason: the provenance the requeue applied."""
    if result["regressed"]:
        return "cli_regress"
    if result["from_cancelled_applied"]:
        return "cli_requeue_from_cancelled"
    if result["from_failed_applied"]:
        return "cli_requeue_from_failed"
    if result["from_completed_applied"]:
        return "cli_requeue_from_completed"
    return "cli_requeue"


def echo_requeue_outcome(
    ticket_id: str, client: str, result: dict[str, str | bool | int]
) -> None:
    """Record ``TICKET_REQUEUED``, announce an inferred regress, echo the row."""
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": ticket_id,
            "client": client,
            "from_stage": result["from_stage"],
            "to_stage": result["to_stage"],
            "reason": _requeue_reason(result),
            "regressed": result["regressed"],
            **(
                {"regress_attempts": result["regress_attempts"]}
                if result["regressed"]
                else {}
            ),
            # #2695: only when --reset-attempts was passed, so payloads
            # without it stay byte-identical.
            **(
                {
                    "attempts_reset": True,
                    "prior_unproductive_attempts": result[
                        "prior_unproductive_attempts"
                    ],
                }
                if result["attempts_reset"]
                else {}
            ),
        },
    )
    if result["regress_inferred"]:
        to_stage = str(result["to_stage"])
        click.echo(
            f"Inferred --regress: '{to_stage}' is behind '{result['from_stage']}'."
        )
        click.echo(f"  regress_attempts is now {result['regress_attempts']}.")
        evidence = _REGRESS_EVIDENCE.get(to_stage)
        if evidence is not None:
            click.echo(evidence)
    click.echo(
        f"Requeued {ticket_id} ({client}):"
        f" {result['from_stage']} -> {result['to_stage']} (PENDING)"
        f"{_reset_suffix(result)}"
    )
    echo_resulting_row(ticket_id, client)


def _reset_suffix(result: dict[str, str | bool | int]) -> str:
    """The confirmation-line suffix for --reset-attempts (#2695 R3)."""
    if not result["attempts_reset"]:
        return ""
    prior = int(result["prior_unproductive_attempts"])
    if result["attempts_reset_was_capped"] and prior > 0:
        return f"; unproductive attempts reset (was {prior})"
    return "; unproductive attempts reset"
