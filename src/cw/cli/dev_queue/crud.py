"""Dev-queue mutation commands: add, move, approve, requeue, unblock, remove,
cancel, clear."""

from __future__ import annotations

from typing import Literal

import click
from pydantic import ValidationError

from cw.cli._base import handle_errors
from cw.config import load_orchestrator_config, load_state
from cw.dev_queue import (
    add_ticket,
    approve_ticket,
    cancel_ticket,
    clear_tickets,
    move_ticket,
    remove_ticket,
    requeue_ticket,
    resolve_client,
    unblock_ticket,
)
from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client

from ._group import dev_queue


@dev_queue.command(name="add")
@click.argument("tickets", nargs=-1, required=True)
@click.option("--client", "-c", default=None, help="Target client name.")
@click.option("--priority", "-p", type=int, default=0, help="Priority (higher=sooner).")
@click.option(
    "--timeout",
    "-t",
    "headless_timeout_override",
    type=int,
    default=None,
    help="Override headless timeout (seconds) for this ticket.",
)
@click.option(
    "--scope",
    "-s",
    "scope_hint",
    type=click.Choice(["small", "large"]),
    default=None,
    help=(
        "Scope tier for headless budget resolution. Used as a fallback when the "
        "session has no prior result (pre-Stage-1). Accepts 'small' or 'large'."
    ),
)
@click.option(
    "--lane",
    "lane_name",
    default=DEFAULT_LANE,
    show_default=True,
    help="Target lane name (must be declared for the client).",
)
@click.option(
    "--signoff",
    "signoff",
    type=click.Choice(["operator"]),
    default=None,
    help=(
        "Require an explicit operator signoff before this ticket ships,"
        " overriding the lane/global default (RFC 0007 Phase 3)."
    ),
)
@handle_errors
def dev_queue_add(
    tickets: tuple[str, ...],
    client: str | None,
    priority: int,
    headless_timeout_override: int | None,
    scope_hint: str | None,
    lane_name: str,
    signoff: Literal["operator"] | None,
) -> None:
    """Enqueue one or more tickets for dispatch."""
    config = load_orchestrator_config()
    for ticket_id in tickets:
        resolved = resolve_client(ticket_id, config, client)
        try:
            task = TicketTask(
                ticket_id=ticket_id,
                client=resolved,
                priority=priority,
                headless_timeout_override=headless_timeout_override,
                scope_hint=scope_hint,
                lane=lane_name,
                signoff=signoff,
            )
        except ValidationError as exc:
            msg = f"Invalid ticket '{ticket_id}': {exc.errors()[0]['msg']}"
            raise click.ClickException(msg) from exc
        inserted = add_ticket(task)
        if not inserted:
            click.echo(
                f"Skipped {ticket_id} -> {resolved}: already queued"
                " (pending, running, completed, or cancelled).",
                err=True,
            )
            continue
        record_event(
            OrchestratorEventType.TICKET_ENQUEUED,
            {"ticket_id": ticket_id, "client": resolved, "priority": priority},
        )
        click.echo(f"Enqueued {ticket_id} -> {resolved} (priority={priority})")


@dev_queue.command(name="move", help="Move a ticket to a different lane.")
@click.argument("ticket_id")
@click.option("--client", "-c", required=True, help="Client name.")
@click.option("--to", "to_lane", required=True, help="Target lane name.")
@handle_errors
def dev_queue_move(ticket_id: str, client: str, to_lane: str) -> None:
    """Move TICKET_ID to a different lane within CLIENT.

    Only PENDING tickets can be moved; RUNNING and BLOCKED_ON_USER tasks
    must be resolved before lane reassignment.
    """
    from_lane = move_ticket(ticket_id, client, to_lane)
    record_event(
        OrchestratorEventType.TICKET_MOVED,
        {
            "ticket_id": ticket_id,
            "client": client,
            "from_lane": from_lane,
            "to_lane": to_lane,
        },
    )
    click.echo(f"Moved {ticket_id} ({client}): {from_lane} -> {to_lane}")


@dev_queue.command(name="approve")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@handle_errors
def dev_queue_approve(ticket_id: str, client: str | None) -> None:
    """Approve a plan/review gate, or clear an operator-signoff gate.

    The ticket must be BLOCKED_ON_USER with last_result status of
    plan_pending_approval or review_pending_approval, or already parked
    AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3). Approving a REVIEW-stage
    gate on a ticket with signoff configured re-routes it to
    AWAITING_OPERATOR_SIGNOFF instead of advancing -- run `approve` again
    to clear it.
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    result = approve_ticket(ticket_id, resolved)
    record_event(
        OrchestratorEventType.TICKET_APPROVED,
        {
            "ticket_id": ticket_id,
            "client": resolved,
            "from_stage": result["from_stage"],
            "to_stage": result["to_stage"],
            "awaiting_signoff": result["awaiting_signoff"],
            "plan_requeued": result["plan_requeued"],
        },
    )
    if result["awaiting_signoff"]:
        click.echo(
            f"Approved {ticket_id} ({resolved}): parked at"
            f" {result['from_stage']} awaiting operator signoff before it ships."
            " Run 'approve' again to clear the gate."
        )
    elif result["plan_requeued"]:
        click.echo(
            f"Approved {ticket_id} ({resolved}): plan not yet quality-reviewed"
            " — re-queued at plan stage to run Plan Quality Review."
            " Re-run auto-dev-plan (or dispatch) to proceed."
        )
    else:
        click.echo(
            f"Approved {ticket_id} ({resolved}):"
            f" {result['from_stage']} -> {result['to_stage']}"
        )


@dev_queue.command(name="requeue")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@click.option(
    "--stage",
    "stage_override",
    type=click.Choice(["plan", "impl", "review", "finalize"]),
    default=None,
    help="Stage to requeue at (default: current stage). Forward-only.",
)
@click.option(
    "--regress",
    "regress",
    is_flag=True,
    default=False,
    help="Allow a backward --stage target on a blocked ticket (e.g. review->impl).",
)
@click.option(
    "--from-cancelled",
    "from_cancelled",
    is_flag=True,
    default=False,
    help=(
        "Allow requeuing a CANCELLED ticket back to PENDING at its current"
        " stage (e.g. after `cw spawn close --confirmed-dead` on a RUNNING"
        " row). Accepts any CANCELLED row regardless of why it was"
        " cancelled — check `cw dev-queue show` / event history first if"
        " the ticket may have been deliberately killed. See"
        " docs/dispatch-runbook.md."
    ),
)
@click.option(
    "--from-failed",
    "from_failed",
    is_flag=True,
    default=False,
    help=(
        "Allow requeuing a FAILED ticket back to PENDING at its current"
        " stage (e.g. an abandoned row whose underlying session actually"
        " completed clean). Accepts any FAILED row regardless of why it"
        " failed — check `cw dev-queue show` / event history first. See"
        " docs/dispatch-runbook.md."
    ),
)
@handle_errors
def dev_queue_requeue(
    ticket_id: str,
    client: str | None,
    stage_override: str | None,
    regress: bool,
    from_cancelled: bool,
    from_failed: bool,
) -> None:
    """Requeue a BLOCKED_ON_USER ticket back to PENDING.

    Defaults to re-running the current stage. Use --stage to advance forward.
    Use --regress with a backward --stage to move a blocked ticket backward
    (e.g. a plan-deviation review exit back to impl). Use --from-cancelled
    to recover a CANCELLED ticket, or --from-failed to recover a FAILED
    ticket (forward/same-stage only).
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    result = requeue_ticket(
        ticket_id,
        resolved,
        stage_override,
        allow_regress=regress,
        from_cancelled=from_cancelled,
        from_failed=from_failed,
    )
    if result["regressed"]:
        reason = "cli_regress"
    elif result["from_cancelled_applied"]:
        reason = "cli_requeue_from_cancelled"
    elif result["from_failed_applied"]:
        reason = "cli_requeue_from_failed"
    else:
        reason = "cli_requeue"
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": ticket_id,
            "client": resolved,
            "from_stage": result["from_stage"],
            "to_stage": result["to_stage"],
            "reason": reason,
            "regressed": result["regressed"],
            **(
                {"regress_attempts": result["regress_attempts"]}
                if result["regressed"]
                else {}
            ),
        },
    )
    click.echo(
        f"Requeued {ticket_id} ({resolved}):"
        f" {result['from_stage']} -> {result['to_stage']} (PENDING)"
    )


@dev_queue.command(name="unblock")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@handle_errors
def dev_queue_unblock(ticket_id: str, client: str | None) -> None:
    """Clear salvage/park markers and requeue a SALVAGE_PARKED ticket.

    The ticket must be BLOCKED_ON_USER with a SALVAGE_PARKED session.
    Clears both last_result and reap_reason on the session, then
    sets the task back to PENDING.
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    unblock_ticket(ticket_id, resolved)
    record_event(
        OrchestratorEventType.TICKET_UNBLOCKED,
        {"ticket_id": ticket_id, "client": resolved},
    )
    click.echo(f"Unblocked {ticket_id} ({resolved}): cleared park markers, PENDING")


@dev_queue.command(name="remove")
@click.argument("tickets", nargs=-1, required=True)
@click.option("--client", "-c", "client", required=True, help="Client name")
@click.option(
    "--all",
    "-a",
    "remove_all",
    is_flag=True,
    default=False,
    help="Remove all matching entries when multiple match",
)
@handle_errors
def dev_queue_remove(tickets: tuple[str, ...], client: str, remove_all: bool) -> None:
    """Remove dev-queue task(s) for the given ticket(s) and client."""
    for ticket in tickets:
        remove_ticket(ticket, client, remove_all=remove_all)
        click.echo(f"Removed {ticket} from {client} dev-queue.")


@dev_queue.command(name="cancel")
@click.argument("tickets", nargs=-1, required=True)
@click.option("--client", "-c", "client", required=True, help="Client name")
@handle_errors
def dev_queue_cancel(tickets: tuple[str, ...], client: str) -> None:
    """Cancel dev-queue task(s) and stop any running session."""
    state = load_state()
    daemon = get_native_daemon_client()
    for ticket in tickets:
        cleared_session_ids = cancel_ticket(ticket, client)
        for old_session_id in cleared_session_ids:
            if old_session_id is not None:
                sess = state.find_by_name_or_id(old_session_id)
                if (
                    sess is not None
                    and sess.surface_ref is not None
                    and sess.origin is SessionOrigin.DAEMON
                ):
                    daemon.stop(sess.surface_ref)
        click.echo(f"Cancelled {ticket} in {client} dev-queue.")


@dev_queue.command(name="clear")
@click.option("--client", "-c", "client", required=True, help="Client name")
@click.option(
    "--status",
    "-s",
    "status_filter",
    type=click.Choice([e.value for e in QueueItemStatus]),
    default=None,
    help="Optional status filter",
)
@handle_errors
def dev_queue_clear(client: str, status_filter: str | None) -> None:
    """Clear dev-queue tasks for the given client, optionally filtered by status."""
    status_enum = QueueItemStatus(status_filter) if status_filter else None
    count = clear_tickets(client, status=status_enum)
    click.echo(f"Cleared {count} dev-queue task(s) for {client}.")
