"""What ``cw dev-queue requeue`` prints and records once the requeue ran (#2692).

Split out of ``crud`` to keep that module under the ~1000-line ceiling:
the idempotent PENDING no-op echo (with its read-only live-session notes),
the inferred-``--regress`` announcement, the ``--reset-attempts`` confirmation
(#2695), and the ``TICKET_REQUEUED`` event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from cw.dev_queue.adoption import (
    ADOPTED_ANCESTRY_RESULT_KEY,
    ANCESTRY_NOT_DESCENDANT,
    ANCESTRY_UNKNOWN,
    record_recheck_pin,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, Stage

from ._settle import live_session_notes
from .show import echo_resulting_row

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    ticket_id: str,
    client: str,
    result: dict[str, str | bool | int],
    *,
    reason: str | None = None,
    extra_payload: Mapping[str, object] | None = None,
) -> None:
    """Record ``TICKET_REQUEUED``, announce an inferred regress, echo the row.

    *reason* replaces the derived provenance and *extra_payload* is merged
    last (``recover-red-pr``, #2945); with neither the payload is unchanged.
    """
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": ticket_id,
            "client": client,
            "from_stage": result["from_stage"],
            "to_stage": result["to_stage"],
            "reason": reason if reason is not None else _requeue_reason(result),
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
            # #2943: only on an adopted re-pin, so other payloads are unchanged.
            **(
                {
                    "adopted_head_sha_old": result["adopted_head_sha_old"],
                    "adopted_head_sha": result["adopted_head_sha"],
                }
                if result.get("adopted_repinned")
                else {}
            ),
            # #2970: only on an intake re-check re-pin.
            **(
                {ADOPTED_ANCESTRY_RESULT_KEY: result[ADOPTED_ANCESTRY_RESULT_KEY]}
                if ADOPTED_ANCESTRY_RESULT_KEY in result
                else {}
            ),
            **(extra_payload or {}),
        },
    )
    if ADOPTED_ANCESTRY_RESULT_KEY in result:
        # After the lock: requeue_ticket saved the row and emits nothing; a
        # crash between the save and here loses only this audit event.
        record_recheck_pin(
            client,
            ticket_id,
            old_sha=str(result["adopted_head_sha_old"]),
            new_sha=str(result["adopted_head_sha"]),
            ancestry_result=str(result[ADOPTED_ANCESTRY_RESULT_KEY]),
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
    if result.get("adopted_repinned"):
        click.echo(
            f"  Re-pinned the adopted head {result['adopted_head_sha_old']}"
            f" -> {result['adopted_head_sha']}{_repin_suffix(result)}"
        )
    echo_resulting_row(ticket_id, client)


def _repin_suffix(result: dict[str, str | bool | int]) -> str:
    """The re-pin line's ending: what the re-check decided about ancestry (#2970).

    A rewritten head says so; an undecidable one says it is unverified;
    descendant, same and the explicit ``--repin-adopted-head`` path (which
    records no ancestry) keep the plain period.
    """
    ancestry = result.get(ADOPTED_ANCESTRY_RESULT_KEY)
    if ancestry == ANCESTRY_NOT_DESCENDANT:
        return " (history rewritten: the new head does not descend from the pin)"
    if ancestry == ANCESTRY_UNKNOWN:
        return (
            " (ancestry could not be decided: the new head may not descend"
            " from the pin)"
        )
    return "."


def _reset_suffix(result: dict[str, str | bool | int]) -> str:
    """The confirmation-line suffix for --reset-attempts (#2695 R3)."""
    if not result["attempts_reset"]:
        return ""
    prior = int(result["prior_unproductive_attempts"])
    if result["attempts_reset_was_capped"] and prior > 0:
        return f"; unproductive attempts reset (was {prior})"
    return "; unproductive attempts reset"
