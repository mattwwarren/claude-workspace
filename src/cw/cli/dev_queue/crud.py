"""Dev-queue mutation commands: add, move, approve, requeue, unblock, remove,
cancel, clear."""

from __future__ import annotations

from typing import Literal

import click
from pydantic import ValidationError

from cw.cli._base import handle_errors
from cw.config import get_client, load_orchestrator_config, load_state
from cw.dev_queue import (
    add_ticket,
    approve_ticket,
    cancel_ticket,
    clear_tickets,
    drain_held_tickets,
    move_ticket,
    remove_ticket,
    requeue_ticket,
    resolve_client,
    unblock_ticket,
)
from cw.events import record_event
from cw.gh import _FETCH_COMMENTS_TIMEOUT, _fetch_issue_comments, post_issue_comment
from cw.models import (
    DEFAULT_LANE,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client
from cw.worktree import _git_dir

from ._group import dev_queue

# Operator-facing human-signoff marker the auto-dev-plan runbooks document
# (README.md, docs/dispatch-runbook.md, docs/session-disposition.md) --
# distinct from lifecycle.py's `_PLAN_SPEC_MARKER`/`_PLAN_SOUNDNESS_MARKER`
# pair (the coded plan-quality-review gate). See GitHub #1419.
_PLAN_APPROVED_MARKER = "<!-- auto-dev-plan-approved -->"


@dev_queue.command(name="add")
@click.argument("tickets", nargs=-1, required=True)
@click.option("--client", "-c", default=None, help="Target client name.")
@click.option("--priority", "-p", type=int, default=0, help="Priority (higher=sooner).")
@click.option(
    "--scope",
    "-s",
    "scope_hint",
    type=click.Choice(["small", "large"]),
    default=None,
    help="Scope tier hint for this ticket. Accepts 'small' or 'large'.",
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
@click.option(
    "--hold-finalize",
    "hold_finalize",
    is_flag=True,
    default=False,
    help=(
        "Stop this ticket before an unattended finalize, overriding the"
        " lane/global default. Parks it BLOCKED_ON_USER with disposition"
        " 'finalize_gate_held'; release with `cw dev-queue approve`"
        " (RFC 0011 A3)."
    ),
)
@handle_errors
def dev_queue_add(
    tickets: tuple[str, ...],
    client: str | None,
    priority: int,
    scope_hint: str | None,
    lane_name: str,
    signoff: Literal["operator"] | None,
    hold_finalize: bool,
) -> None:
    """Enqueue one or more tickets for dispatch."""
    config = load_orchestrator_config()
    # The flag has exactly one "on" value, so it is a boolean switch at the CLI
    # (the --post-marker shape) rather than a click.Choice like --signoff;
    # translate it to the model's Literal here, at the single call site.
    hold_finalize_value: Literal["manual"] | None = "manual" if hold_finalize else None
    for ticket_id in tickets:
        resolved = resolve_client(ticket_id, config, client)
        try:
            task = TicketTask(
                ticket_id=ticket_id,
                client=resolved,
                priority=priority,
                scope_hint=scope_hint,
                lane=lane_name,
                signoff=signoff,
                hold_finalize=hold_finalize_value,
            )
        except ValidationError as exc:
            msg = f"Invalid ticket '{ticket_id}': {exc.errors()[0]['msg']}"
            raise click.ClickException(msg) from exc
        inserted = add_ticket(task)
        if not inserted:
            click.echo(
                f"Skipped {ticket_id} -> {resolved}: already queued"
                " (pending, running, completed, or cancelled) or parked"
                " awaiting the operator — a parked row is released with"
                " `cw dev-queue requeue` or `cw dev-queue approve`, never"
                " by re-adding (#1653).",
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


def _post_plan_approved_marker(
    ticket_id: str, resolved: str, result: dict[str, str | bool]
) -> bool:
    """Post (or dedup-skip) the plan-approved marker for ``--post-marker``.

    PLAN-stage only -- warns and returns False on any other stage. Returns
    True iff the marker is recorded on the issue after this call (either
    found already present, or just posted successfully); False on every
    warn / fail-closed / gh-failure path. See GitHub #1419.
    """
    if result["from_stage"] != "plan":
        click.echo(
            f"--post-marker is PLAN-stage-only; ticket is at"
            f" {result['from_stage']!r} — marker not posted.",
            err=True,
        )
        return False

    repo_cwd = _git_dir(get_client(resolved))
    comments = _fetch_issue_comments(
        ticket_id, timeout=_FETCH_COMMENTS_TIMEOUT, cwd=repo_cwd
    )
    if comments is None:
        click.echo(
            "--post-marker: could not verify existing comments — marker"
            " not posted (dedup check failed)."
        )
        return False

    if any(
        isinstance(c.get("body"), str) and _PLAN_APPROVED_MARKER in c["body"]
        for c in comments
    ):
        click.echo(
            f"--post-marker: plan-approved marker already present on"
            f" {ticket_id} ({resolved}) — skipped (no duplicate posted)."
        )
        return True

    post_result = post_issue_comment(ticket_id, _PLAN_APPROVED_MARKER, cwd=repo_cwd)
    if post_result is not None and post_result.returncode == 0:
        click.echo(
            f"--post-marker: posted the plan-approved marker comment"
            f" to {ticket_id} ({resolved})."
        )
        return True

    click.echo(
        f"--post-marker: failed to post the plan-approved marker"
        f" comment to {ticket_id} ({resolved}) — see gh error"
        " above.",
        err=True,
    )
    return False


@dev_queue.command(name="approve")
@click.argument("ticket_id")
@click.option("--client", "-c", default=None, help="Client name.")
@click.option(
    "--post-marker",
    "post_marker",
    is_flag=True,
    default=False,
    help=(
        "Post the human-signoff marker comment"
        " (<!-- auto-dev-plan-approved -->) to the ticket, confirming"
        " operator sign-off on a PLAN-stage checkpoint. PLAN-stage only"
        " (warns and skips on other stages). Distinct from `add"
        " --signoff`, which requires operator signoff before a ticket"
        " ships, and from this command's own REVIEW-stage"
        " operator-signoff gate (see docstring above) — this flag only"
        " posts an audit-trail comment."
    ),
)
@handle_errors
def dev_queue_approve(ticket_id: str, client: str | None, post_marker: bool) -> None:
    """Approve a plan/review gate, or clear an operator-signoff gate.

    The ticket must be BLOCKED_ON_USER with last_result status of
    plan_pending_approval or review_pending_approval, or already parked
    AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3). Approving a REVIEW-stage
    gate on a ticket with signoff configured re-routes it to
    AWAITING_OPERATOR_SIGNOFF instead of advancing -- run `approve` again
    to clear it.

    Pass --post-marker to also post the plan-approved audit marker on a
    PLAN-stage ticket (unrelated to the operator-signoff gate above or to
    `add --signoff`).
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
    marker_already_recorded = False
    if post_marker:
        marker_already_recorded = _post_plan_approved_marker(
            ticket_id=ticket_id, resolved=resolved, result=result
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
        if not marker_already_recorded:
            click.echo(
                "Pass --post-marker to also post the plan-approved audit"
                " marker comment on this ticket."
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


@dev_queue.command(name="drain")
@click.option(
    "--held",
    "held_flag",
    is_flag=True,
    required=True,
    help=(
        "Required. Drains every Rule-5 availability-park ticket"
        " (disposition=awaiting_operator) back to PENDING at its own"
        " current stage. Does NOT release A3 force-holds"
        " (proactive stop-before-finalize) -- those release only via"
        " `cw dev-queue approve <ticket>`, since their gate checks caller"
        " provenance. Morning routine is two commands: this one for"
        " availability parks, `approve` per deliberate force-hold."
    ),
)
@click.option("--client", "-c", "client", required=True, help="Client name.")
@click.option("--lane", "lane_name", default=None, help="Restrict to a single lane.")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="List what would be drained without mutating anything.",
)
@handle_errors
def dev_queue_drain(
    held_flag: bool, client: str, lane_name: str | None, dry_run: bool
) -> None:
    """Resume every held (Rule-5 availability-park) ticket for CLIENT.

    Re-fires each selected ticket through requeue_ticket at its own current
    stage (RFC 0011 A4). A3 force-holds are out of scope -- release those with
    `cw dev-queue approve <ticket>` instead. See docs/dispatch-runbook.md.
    """
    del held_flag  # required flag; no other mode exists yet (R6)
    outcomes = drain_held_tickets(client, lane=lane_name, dry_run=dry_run)
    if not outcomes:
        suffix = f" (lane={lane_name})" if lane_name else ""
        click.echo(f"No held tickets to drain for {client}{suffix}.")
        return
    failed = 0
    for outcome in outcomes:
        if outcome["status"] == "requeued":
            record_event(
                OrchestratorEventType.TICKET_REQUEUED,
                {
                    "ticket_id": outcome["ticket_id"],
                    "client": client,
                    "from_stage": outcome["from_stage"],
                    "to_stage": outcome["to_stage"],
                    "reason": "cli_drain_held",
                    "regressed": False,
                },
            )
            click.echo(
                f"Drained {outcome['ticket_id']} ({client}): {outcome['detail']}"
            )
        elif outcome["status"] == "would_requeue":
            click.echo(
                f"[dry-run] Would drain {outcome['ticket_id']} ({client})"
                f" at {outcome['detail']}."
            )
        else:
            failed += 1
            click.echo(
                f"Failed to drain {outcome['ticket_id']} ({client}):"
                f" {outcome['detail']}",
                err=True,
            )
    if failed:
        msg = (
            f"{failed} of {len(outcomes)} held ticket(s) failed to drain for {client}."
        )
        raise click.ClickException(msg)


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
