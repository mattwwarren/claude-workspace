"""Orchestrator development-queue commands (``dev-queue`` group)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Literal

import click
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.cli._base import _complete_client, _emit_freshness_subline, handle_errors, main
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.config import (
    _load_concurrency_overrides,
    get_client,
    load_clients,
    load_orchestrator_config,
    load_state,
)
from cw.dev_queue import (
    _find_ticket,
    add_ticket,
    approve_ticket,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    load_dev_queue,
    move_ticket,
    remove_ticket,
    requeue_ticket,
    resolve_client,
    unblock_ticket,
    wait_for_terminal,
)
from cw.dispatch import (
    TICK_STALE_SECONDS,
    run_dispatch_loop,
)
from cw.dispatch_serve import run_dispatch_serve
from cw.events import record_event
from cw.exceptions import CwError, MissingWorkspaceError, WorktreeError
from cw.models import (
    DEFAULT_LANE,
    OCCUPIED_LANE_STATUSES,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    TicketTask,
)
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client
from cw.orchestrate import latest_tick_summary_by_client
from cw.plan import run_planner
from cw.reconcile import (
    _csid_from_transcript,
    _transcript_age_seconds,
    resolve_idle_watchdog_budget,
)
from cw.session import _is_native_surface_ref
from cw.worktree import fast_forward_main

# Statuses considered "active" for default filtering in list / status.
_ACTIVE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.PENDING,
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    }
)

# Suffix appended to a lane breakdown line when that lane is paused (operator or
# circuit breaker). Pinned exact string — asserted verbatim in tests. See #875.
_PAUSED_LANE_MARKER = " [PAUSED]"


@main.group(name="dev-queue")
def dev_queue() -> None:
    """Manage the orchestrator development queue."""


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


def _lane_caps_for_client(
    client_name: str, config: OrchestratorConfig
) -> dict[str, int]:
    """Resolve each lane's effective cap for *client_name* (dev-queue status rendering).

    Mirrors dispatch_tick's no-lanes override (dispatch.py's client loop): a
    client with no declared lanes is capped at the client ceiling, not
    LaneConfig's max_parallel=1 default. Falls back to a single default-lane
    entry at the ceiling when the client isn't found in clients.yaml -- dev-queue
    status must render even for a client dropped from config while tickets remain
    queued. See #1243.
    """
    ceiling = config.per_client_ceiling.get(client_name, config.default_ceiling)
    try:
        client = get_client(client_name)
    except CwError:
        return {DEFAULT_LANE: ceiling}
    if not client.lanes:
        return {DEFAULT_LANE: ceiling}
    return {lane.name: lane.max_parallel for lane in client.lanes}


def _emit_dev_queue_lane_breakdown(
    tasks: list[TicketTask], client_name: str, config: OrchestratorConfig
) -> None:
    """Print indented lane lines; occupant line(s) when a lane is noteworthy.

    A lane is noteworthy when it has non-RUNNING occupied status count > 0
    (blocked/signoff) OR is at/over its effective cap -- i.e. exactly when a
    reader could misdiagnose why nothing is claiming (#1243). For a
    single-default-lane client, the base + occupant lines are suppressed
    entirely unless the lane is noteworthy (unchanged quiet-case behaviour);
    for multi-/named-lane clients the base line still prints unconditionally per
    lane as before, with the occupant line added only when that lane is
    noteworthy.
    """
    lanes_seen: set[str] = {t.lane for t in tasks}
    single_default_lane = len(lanes_seen) <= 1 and lanes_seen <= {DEFAULT_LANE}
    lane_caps = _lane_caps_for_client(client_name, config)
    overrides = _load_concurrency_overrides()
    by_lane: dict[str, list[TicketTask]] = {}
    for task in tasks:
        by_lane.setdefault(task.lane, []).append(task)
    for lane_name in sorted(by_lane):
        lane_tasks = by_lane[lane_name]
        pending = sum(1 for t in lane_tasks if t.status == QueueItemStatus.PENDING)
        running = sum(1 for t in lane_tasks if t.status == QueueItemStatus.RUNNING)
        blocked = sum(
            1 for t in lane_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER
        )
        signoff = sum(
            1
            for t in lane_tasks
            if t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        )
        occupants = [t for t in lane_tasks if t.status in OCCUPIED_LANE_STATUSES]
        cap = lane_caps.get(lane_name, 1)
        at_cap = len(occupants) >= cap
        noteworthy = (blocked + signoff) > 0 or at_cap
        if single_default_lane and not noteworthy:
            continue
        override = overrides.lanes.get(f"{lane_tasks[0].client}/{lane_name}")
        marker = _PAUSED_LANE_MARKER if override is not None and override.paused else ""
        click.echo(
            f"    lane {lane_name}:"
            f" pending={pending} running={running} blocked={blocked}"
            f" signoff={signoff}{marker}"
        )
        if noteworthy:
            occupant_str = ", ".join(
                f"{t.ticket_id} ({t.status.value})" for t in occupants
            )
            # "lane full" only when actually at/over cap -- a lane with spare
            # capacity that merely has a blocked/signoff occupant is not full,
            # and mislabeling it that way reintroduces the same misdiagnosis
            # risk (#1243) on a lane that could still claim more work.
            label = "lane full" if at_cap else "lane occupants"
            click.echo(f"      {label}: {occupant_str}")


@dev_queue.command(name="status")
@click.option("--client", "-c", default=None, help="Filter by client.")
@click.option("--json", "output_json", is_flag=True, help="JSON dict keyed by client.")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Include completed/cancelled tickets in the TICKETS column (legacy view).",
)
@handle_errors
def dev_queue_status(client: str | None, output_json: bool, show_all: bool) -> None:
    """Show dev queue status grouped by client."""
    if output_json:
        tick_data = latest_tick_summary_by_client()
        attn_by_client = _needs_attn_by_client(list_tickets(client))
        click.echo(
            json.dumps(
                {
                    c: {
                        "skip_reason": tick.skip_reason,
                        "freshness_detail": tick.freshness_detail,
                        "blocked_branch": tick.blocked_branch,
                        "needs_attn": attn_by_client.get(c, 0),
                    }
                    for c, tick in tick_data.items()
                    if client is None or c == client
                }
            )
        )
        return

    tasks = list_tickets(client)

    if not tasks:
        click.echo("Dev queue is empty.")
        return

    # Group by client
    clients_seen: list[str] = []
    by_client: dict[str, list[TicketTask]] = {}
    for task in tasks:
        if task.client not in by_client:
            clients_seen.append(task.client)
            by_client[task.client] = []
        by_client[task.client].append(task)

    header = (
        f"{'CLIENT':<20} {'PENDING':>7}  {'RUNNING':>7}  {'BLOCKED':>7}"
        f"  {'COMPLETED':>9}  {'CANCELLED':>9}  {'NEEDS_ATTN':>10}  TICKETS"
    )
    click.echo(header)
    click.echo("-" * 90)
    for client_name in clients_seen:
        client_tasks = by_client[client_name]
        pending_tasks = [t for t in client_tasks if t.status == QueueItemStatus.PENDING]
        running_tasks = [t for t in client_tasks if t.status == QueueItemStatus.RUNNING]
        blocked_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER
        ]
        completed_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.COMPLETED
        ]
        cancelled_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.CANCELLED
        ]
        needs_attn = _count_needs_attn(client_tasks)
        display_tasks = (
            client_tasks
            if show_all
            else [t for t in client_tasks if t.status in _ACTIVE_STATUSES]
        )
        ticket_ids = ", ".join(t.ticket_id for t in display_tasks) or "—"
        click.echo(
            f"{client_name:<20} {len(pending_tasks):>7}  {len(running_tasks):>7}"
            f"  {len(blocked_tasks):>7}  {len(completed_tasks):>9}"
            f"  {len(cancelled_tasks):>9}  {needs_attn:>10}  {ticket_ids}"
        )

    tick_data = latest_tick_summary_by_client()
    if tick_data:
        config = load_orchestrator_config()
        click.echo("")
        click.echo("Last dispatch tick per client:")
        click.echo(
            "  (snapshot from the most recent dispatch tick"
            " — not live queue state; see the table above)"
        )
        now = datetime.now(UTC)
        for client_name in clients_seen:
            if client_name in tick_data:
                tick = tick_data[client_name]
                tick_line = (
                    f"  {client_name}: claimed={tick.claimed}  pending={tick.pending}"
                    f"  running={tick.running}/{tick.cap}  occupied={tick.occupied}"
                    f"  skip={tick.skip_reason}"
                )
                age_secs = (now - tick.tick_at).total_seconds()
                age = int(age_secs)
                if age_secs > TICK_STALE_SECONDS:
                    tick_line += f" [STALE — no tick in {age}s]"
                click.echo(tick_line)
                if tick.skip_reason == DispatchSkipReason.FRESHNESS_GATE:
                    n_pending = sum(
                        1
                        for t in by_client[client_name]
                        if t.status == QueueItemStatus.PENDING
                    )
                    _emit_freshness_subline(
                        client_name,
                        tick.freshness_detail,
                        tick.blocked_branch,
                        n_pending,
                    )
                _emit_dev_queue_lane_breakdown(
                    by_client[client_name], client_name, config
                )


@dev_queue.command(name="run")
@click.option(
    "--max-parallel",
    "-p",
    default=None,
    type=int,
    help="Override per-client concurrency cap.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single dispatch tick and exit.",
)
@click.option(
    "--use-plan",
    is_flag=True,
    default=False,
    help="Respect the persisted DispatchPlan ordering when claiming tasks.",
)
@click.option(
    "--parent",
    default=None,
    help=(
        "Orchestrator session ID. Spawned workers are linked back via "
        "parent_session_id + worker_session_ids."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress per-tick operator output (for cron/scripted use).",
)
@click.option(
    "--auto-ff/--no-auto-ff",
    "auto_ff",
    default=True,
    help="Disable automatic fast-forward of local main (legacy block-only behavior).",
)
@click.option(
    "--client",
    "-c",
    default=None,
    help="Dispatch only this client's queue.",
)
@handle_errors
def dev_queue_run(
    max_parallel: int | None,
    once: bool,
    use_plan: bool,
    parent: str | None,
    quiet: bool,
    auto_ff: bool,
    client: str | None,
) -> None:
    """Run the dispatch loop, spawning sessions for pending tickets."""
    if client is not None:
        get_client(client)
    run_dispatch_loop(
        max_parallel=max_parallel,
        once=once,
        use_plan=use_plan,
        parent=parent,
        emit=None if quiet else click.echo,
        auto_ff=auto_ff,
        client=client,
    )


@dev_queue.command(name="serve")
@click.option(
    "--max-parallel",
    "-p",
    default=None,
    type=int,
    help="Override per-client concurrency cap.",
)
@click.option(
    "--use-plan",
    is_flag=True,
    default=False,
    help="Respect the persisted DispatchPlan ordering when claiming tasks.",
)
@click.option(
    "--parent",
    default=None,
    help=(
        "Orchestrator session ID. Spawned workers are linked back via "
        "parent_session_id + worker_session_ids."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress per-tick operator output (for cron/scripted use).",
)
@click.option(
    "--auto-ff/--no-auto-ff",
    "auto_ff",
    default=True,
    help="Disable automatic fast-forward of local main (legacy block-only behavior).",
)
@click.option(
    "--client",
    "-c",
    default=None,
    help="Dispatch only this client's queue.",
)
@click.option(
    "--max-restarts",
    "max_restarts",
    type=int,
    default=-1,
    show_default=True,
    help="Maximum number of restarts. -1 = unlimited.",
)
@handle_errors
def dev_queue_serve(
    max_parallel: int | None,
    use_plan: bool,
    parent: str | None,
    quiet: bool,
    auto_ff: bool,
    client: str | None,
    max_restarts: int,
) -> None:
    """Run the dispatch loop with automatic restart on crash.

    Unlike ``run``, ``serve`` restarts the dispatch loop after crashes with
    exponential backoff. It exits cleanly on Ctrl-C or a normal (non-crash)
    return from the loop. Use this command in place of ``run`` when you want
    a self-healing long-running dispatch process.
    """
    if client is not None:
        get_client(client)
    run_dispatch_serve(
        max_parallel=max_parallel,
        use_plan=use_plan,
        parent=parent,
        emit=None if quiet else click.echo,
        auto_ff=auto_ff,
        client=client,
        max_restarts=max_restarts,
    )


_PLAN_DEFAULT_TIMEOUT = 300

_WAIT_DEFAULT_TIMEOUT: int = 300
_WAIT_EXIT_FAILED: int = 1
_WAIT_EXIT_BLOCKED: int = 2
_WAIT_EXIT_ATTENTION: int = 3
# Ticket parked AWAITING_OPERATOR_SIGNOFF (RFC 0007 Phase 3, #990).
_WAIT_EXIT_SIGNOFF: int = 4
_WAIT_EXIT_TIMEOUT: int = 124

# Poll interval for the sentinel-aware wait loop (seconds).
_WAIT_SENTINEL_POLL_INTERVAL: float = 5.0

# Exit-code mapping from AutoDevResult.status to wait exit codes.
_WAIT_STATUS_EXIT: dict[str, int] = {
    "shipped": 0,
    "no_op": 0,
    "blocked": _WAIT_EXIT_BLOCKED,
    "ambiguities_pending_resolution": _WAIT_EXIT_BLOCKED,
    "premises_pending_verification": _WAIT_EXIT_BLOCKED,
    "plan_pending_approval": _WAIT_EXIT_BLOCKED,
    "review_pending_approval": _WAIT_EXIT_BLOCKED,
    "merge_gate_blocked": _WAIT_EXIT_BLOCKED,
    "scope_exceeded": _WAIT_EXIT_FAILED,
    "forbidden_area": _WAIT_EXIT_FAILED,
    "validation_failed": _WAIT_EXIT_FAILED,
    "failed": _WAIT_EXIT_FAILED,
}


def _blocked_on_user_exit_code(task: TicketTask) -> int:
    """Map a BLOCKED_ON_USER task to its ``dev-queue wait`` exit code.

    Returns ``_WAIT_EXIT_ATTENTION`` when the block originated from a reap
    proposal (the owning session has ``reap_proposed_at`` set, #542), else
    ``_WAIT_EXIT_BLOCKED``.
    """
    if task.session_id is not None:
        state = load_state()
        session = next(
            (s for s in state.sessions if s.id == task.session_id),
            None,
        )
        if session is not None and session.reap_proposed_at is not None:
            return _WAIT_EXIT_ATTENTION
    return _WAIT_EXIT_BLOCKED


def _handle_terminal_task(
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    output_json: bool,
) -> None:
    """Emit terminal-status output and raise the mapped ``Exit`` for *task*.

    Only call when ``task.status`` is one of COMPLETED / FAILED / CANCELLED /
    BLOCKED_ON_USER / AWAITING_OPERATOR_SIGNOFF. COMPLETED returns normally;
    every other terminal status raises ``click.exceptions.Exit``.
    """
    status_str = task.status.value
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": status_str,
                    "session_id": task.session_id,
                    "state": "terminal",
                    "sentinel_status": None,
                    "pr_url": None,
                }
            )
        )
    else:
        click.echo(f"Status: {status_str.upper()}")

    if task.status == QueueItemStatus.COMPLETED:
        return
    if task.status == QueueItemStatus.BLOCKED_ON_USER:
        raise click.exceptions.Exit(_blocked_on_user_exit_code(task))
    if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        raise click.exceptions.Exit(_WAIT_EXIT_SIGNOFF)
    raise click.exceptions.Exit(_WAIT_EXIT_FAILED)


def _handle_sentinel_terminal(
    sentinel: AutoDevResult,
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    output_json: bool,
) -> None:
    """Emit sentinel-terminal output and raise the mapped ``Exit`` for *sentinel*."""
    exit_code = _WAIT_STATUS_EXIT.get(sentinel.status, _WAIT_EXIT_FAILED)
    pr_url = sentinel.pr.url if sentinel.pr is not None else None
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": task.session_id,
                    "state": "terminal",
                    "sentinel_status": sentinel.status,
                    "pr_url": pr_url,
                }
            )
        )
    else:
        click.echo(
            f"Sentinel: {sentinel.status.upper()}" + (f" ({pr_url})" if pr_url else "")
        )
    raise click.exceptions.Exit(exit_code)


def _raise_if_deadline_exceeded(
    deadline: float,
    ticket_id: str,
    resolved: str,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Emit timeout output and raise ``Exit`` when the hard ceiling has passed.

    No-op (returns) while time remains, so callers can fall through to a
    poll-sleep. Used by every poll-and-retry branch in ``dev-queue wait``.
    """
    if time.monotonic() >= deadline:
        _emit_wait_timeout(ticket_id, resolved, timeout_seconds, output_json)
        raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT)


def _handle_attention(
    task: TicketTask,
    session: Session,
    ticket_id: str,
    resolved: str,
    output_json: bool,
    *,
    now: datetime,
    transcript_age: float | None,
) -> None:
    """Emit ATTENTION output and raise ``Exit(_WAIT_EXIT_ATTENTION)``.

    Only called when the attention predicate holds, which guarantees
    ``transcript_age`` is non-None; the ``or 0.0`` is a defensive default.
    """
    elapsed_seconds = (now - session.started_at).total_seconds()
    age = transcript_age if transcript_age is not None else 0.0
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": task.session_id,
                    "state": "attention",
                    "sentinel_status": None,
                    "pr_url": None,
                    "elapsed_seconds": elapsed_seconds,
                    "transcript_age_seconds": transcript_age,
                }
            )
        )
    else:
        click.echo(
            f"ATTENTION: {ticket_id} stalled (transcript {age:.0f}s old, not in roster)"
        )
    raise click.exceptions.Exit(_WAIT_EXIT_ATTENTION)


def _check_stale_attention(
    task: TicketTask,
    session: Session,
    sentinel: AutoDevResult | BlockedResult | None,
    ticket_id: str,
    resolved: str,
    output_json: bool,
    config: OrchestratorConfig,
) -> None:
    """Fire ATTENTION when the session is stale and absent from the daemon roster.

    No-op when the transcript is fresh, the session is in roster, or a
    BlockedResult sentinel is present (partial-write guard).  Raises
    ``Exit(_WAIT_EXIT_ATTENTION)`` when the attention predicate holds.
    """
    now = datetime.now(UTC)
    budget = resolve_idle_watchdog_budget(task, config)
    transcript_age = _transcript_age_seconds(session, now)
    is_stale = transcript_age is not None and transcript_age > budget
    in_roster = False
    surface_ref = session.surface_ref
    if surface_ref is not None and _is_native_surface_ref(surface_ref):
        daemon = get_native_daemon_client()
        in_roster = surface_ref in daemon.list_live_session_short_ids()

    # BlockedResult → keep polling (partial write guard), so exclude from ATTENTION.
    no_sentinel_at_all = sentinel is None
    is_attention = (
        is_stale
        and no_sentinel_at_all
        and surface_ref is not None
        and _is_native_surface_ref(surface_ref)
        and not in_roster
    )
    if is_attention:
        _handle_attention(
            task,
            session,
            ticket_id,
            resolved,
            output_json,
            now=now,
            transcript_age=transcript_age,
        )


def _handle_reaped_mid_wait(
    task: TicketTask,
    ticket_id: str,
    resolved: str,
    last_session_id: str | None,
    output_json: bool,
) -> None:
    """Emit ATTENTION output when a session is reaped during the wait loop.

    Called when session_id transitions from non-None to None mid-wait, which
    means reconcile has reaped the owning session and reverted the task to
    PENDING.  The operator must decide whether to re-dispatch (#542).
    """
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": task.status.value,
                    "session_id": last_session_id,
                    "state": "attention",
                    "reason": "reaped_awaiting_redispatch",
                    "sentinel_status": None,
                    "pr_url": None,
                    "elapsed_seconds": None,
                    "transcript_age_seconds": None,
                }
            )
        )
    else:
        click.echo(f"ATTENTION: {ticket_id} reaped mid-wait (task reverted to PENDING)")
    raise click.exceptions.Exit(_WAIT_EXIT_ATTENTION)


def _emit_wait_timeout(
    ticket_id: str,
    resolved: str,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Emit timeout output for ``dev-queue wait``."""
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": resolved,
                    "status": "timeout",
                    "session_id": None,
                    "state": "timeout",
                    "sentinel_status": None,
                    "pr_url": None,
                }
            )
        )
    else:
        click.echo(f"Timeout waiting for {ticket_id} (>{timeout_seconds:.0f}s)")


def _run_plan_impl(
    *,
    client_name: str,
    timeout: int,
    client_filter: str | None,
    native_daemon: NativeDaemonClient | None = None,
) -> int:
    """Spawn the planner, persist the result, and report status.

    Separated from the Click command so tests can inject a fake native
    daemon client directly.  Returns 0 on success, 1 on validation/timeout
    failure.
    """
    client_config = get_client(client_name)
    result = run_planner(
        client=client_config,
        native_daemon=native_daemon,
        timeout_seconds=timeout,
        client_filter=client_filter,
    )
    plan = result.plan
    if plan is None:
        click.echo(
            f"Planner failed: {result.error} (queue order unchanged)",
            err=True,
        )
        return 1
    click.echo(f"Plan persisted: {len(plan.tasks)} tasks (session {result.session_id})")
    return 0


@dev_queue.command(name="plan")
@click.option(
    "--client",
    "-c",
    required=True,
    shell_complete=_complete_client,
    help="Client whose workspace will host the planner session.",
)
@click.option(
    "--timeout",
    type=int,
    default=_PLAN_DEFAULT_TIMEOUT,
    help="Seconds to wait for the planner JSON output (default: 300).",
)
@click.option(
    "--filter-client",
    default=None,
    help="Only include pending tickets for this client in the planner prompt.",
)
@handle_errors
def dev_queue_plan(client: str, timeout: int, filter_client: str | None) -> None:
    """Spawn /orchestrate-plan to produce a DispatchPlan for pending tickets.

    Runs a one-shot Claude session via cw spawn, waits for it to write a
    DispatchPlan JSON file, validates it, and persists it for use by
    ``cw dev-queue run --use-plan``.

    On validation failure or timeout, the dev queue is left unchanged.
    """
    exit_code = _run_plan_impl(
        client_name=client,
        timeout=timeout,
        client_filter=filter_client,
    )
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)


@dev_queue.command(name="wait")
@click.argument("ticket_id")
@click.option(
    "--client",
    "-c",
    default=None,
    shell_complete=_complete_client,
    help="Client name.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=_WAIT_DEFAULT_TIMEOUT,
    help="Seconds to wait (default: 300).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON output.",
)
@handle_errors
def dev_queue_wait(
    ticket_id: str,
    client: str | None,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Block until a dev-queue ticket reaches terminal status.

    Sentinel-aware: detects AUTO_DEV_RESULT sentinels in the transcript
    directly rather than relying solely on task-status polling.  This
    eliminates false-timeout (exit 124) for long-running healthy workers
    whose reconcile cycle hasn't fired yet.

    Exit codes:
      0   shipped / no_op (or COMPLETED queue status)
      1   scope_exceeded / forbidden_area / failed / FAILED / CANCELLED
      2   blocked / *_pending_* family / BLOCKED_ON_USER (no reap proposal)
      3   ATTENTION — transcript stale past idle budget, worker not in roster;
          or BLOCKED_ON_USER caused by a reap proposal (reap_proposed_at set)
      4   AWAITING_OPERATOR_SIGNOFF — ticket parked for an explicit operator
          signoff before it ships (RFC 0007 Phase 3, #990)
      124 hard timeout ceiling (--timeout) with no terminal or attention signal
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)

    deadline = time.monotonic() + timeout_seconds
    # Track the first non-None session_id seen so we can distinguish a
    # legitimate spawn window (session_id never set yet) from a post-reap
    # revert (session_id was set, then cleared by reconcile — #542).
    observed_session_id: str | None = None

    while True:
        # --- Step 1: fast path — task already terminal in the queue ---
        store = load_dev_queue()
        try:
            task = _find_ticket(store, ticket_id, resolved)
        except CwError:
            task = None
        if task is None:
            # Fallback: delegate to wait_for_terminal so it can surface
            # "not found" errors (CwError) and handle TimeoutError.
            try:
                task = wait_for_terminal(ticket_id, resolved, timeout=timeout_seconds)
            except TimeoutError:
                _emit_wait_timeout(ticket_id, resolved, timeout_seconds, output_json)
                raise click.exceptions.Exit(_WAIT_EXIT_TIMEOUT) from None

        if task.status in {
            QueueItemStatus.COMPLETED,
            QueueItemStatus.FAILED,
            QueueItemStatus.CANCELLED,
            QueueItemStatus.BLOCKED_ON_USER,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        }:
            # COMPLETED returns; every other terminal status raises Exit.
            _handle_terminal_task(task, ticket_id, resolved, output_json)
            return

        # --- Step 2: resolve the session ---
        session_id = task.session_id
        if session_id is None:
            if observed_session_id is not None:
                # session_id was non-None on a prior poll and is now None.
                # Reconcile reaped the session and reverted the task to PENDING;
                # spawn-window grace does not apply — surface ATTENTION (#542).
                _handle_reaped_mid_wait(
                    task, ticket_id, resolved, observed_session_id, output_json
                )
            # Spawn-window grace: session hasn't registered yet — keep polling.
            _raise_if_deadline_exceeded(
                deadline, ticket_id, resolved, timeout_seconds, output_json
            )
            time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
            continue

        observed_session_id = session_id

        cw_state = load_state()
        session = next((s for s in cw_state.sessions if s.id == session_id), None)
        if session is None:
            # Session not in state yet — spawn-window grace, keep polling.
            _raise_if_deadline_exceeded(
                deadline, ticket_id, resolved, timeout_seconds, output_json
            )
            time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)
            continue

        # --- Step 3: resolve claude session id ---
        csid = session.claude_session_id or _csid_from_transcript(session)

        # --- Step 4: parse sentinel from transcript ---
        sentinel: AutoDevResult | BlockedResult | None = None
        if session.worktree_path is not None and csid is not None:
            sentinel = _parse_sentinel_from_transcript(str(session.worktree_path), csid)

        # BlockedResult means framing present but payload unusable — treat as
        # not-yet-terminal (could be a partial write); keep polling.
        if isinstance(sentinel, AutoDevResult):
            # TERMINAL: emit and raise the mapped exit code.
            _handle_sentinel_terminal(sentinel, task, ticket_id, resolved, output_json)

        # --- Step 5: HEARTBEAT / ATTENTION ---
        # ATTENTION: stale AND worker not native OR not in daemon roster.
        # Must guard with _is_native_surface_ref to avoid false-attention on
        # non-daemon surface refs (e.g. tmux window names).
        # BlockedResult → keep polling (partial write guard), so exclude from ATTENTION.
        _check_stale_attention(
            task, session, sentinel, ticket_id, resolved, output_json, config
        )

        # HEARTBEAT: no terminal sentinel but transcript advancing (or session
        # hasn't hit the budget yet) — keep polling within the hard ceiling.
        _raise_if_deadline_exceeded(
            deadline, ticket_id, resolved, timeout_seconds, output_json
        )

        time.sleep(_WAIT_SENTINEL_POLL_INTERVAL)


def _task_to_dict(task: TicketTask) -> dict[str, object]:
    return {
        "ticket_id": task.ticket_id,
        "client": task.client,
        "status": task.status.value,
        "session_id": task.session_id,
        "attempts": task.attempts,
        "priority": task.priority,
        "lane": task.lane,
        "created_at": task.created_at.isoformat(),
        "total_cost_usd": task.total_cost_usd,
        "worktree_path": str(task.worktree_path) if task.worktree_path else None,
        "disposition": task.disposition,
        "pr_url": task.pr_url,
        "pr_state": (
            task.pr_state.model_dump(mode="json") if task.pr_state is not None else None
        ),
        "signoff": task.signoff,
        "last_blocked_result": task.last_blocked_result,
    }


def _task_attention_state(task: TicketTask) -> str | None:
    """The task's hydrated PR attention_state, or None if not hydrated/clean."""
    return task.pr_state.attention_state if task.pr_state is not None else None


def _count_needs_attn(tasks: list[TicketTask]) -> int:
    """Count tasks whose hydrated PR state carries a non-null attention_state."""
    return sum(1 for t in tasks if _task_attention_state(t) is not None)


def _needs_attn_by_client(tasks: list[TicketTask]) -> dict[str, int]:
    """Map client -> count of tasks needing attention (non-null attention_state)."""
    counts: dict[str, int] = {}
    for t in tasks:
        if _task_attention_state(t) is not None:
            counts[t.client] = counts.get(t.client, 0) + 1
    return counts


def _print_tasks_human(tasks: list[TicketTask]) -> None:
    if not tasks:
        click.echo("No tasks found.")
        return
    headers = [
        "TICKET_ID",
        "CLIENT",
        "STATUS",
        "SESSION_ID",
        "ATTEMPTS",
        "LANE",
        "DISPOSITION",
        "PR",
        "ATTENTION",
    ]
    col_widths = [12, 16, 16, 12, 8, 12, 20, 10, 18]
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for t in tasks:
        attention = _task_attention_state(t) or "—"
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:16],
            (t.session_id or "-")[:12],
            str(t.attempts)[:8],
            t.lane[:12],
            (t.disposition or "—")[:20],
            (t.pr_url or "—")[:10],
            attention[:18],
        ]
        click.echo("  ".join(f"{v:<{w}}" for v, w in zip(row, col_widths, strict=True)))


@dev_queue.command(name="tasks")
@click.option("--ticket", "-t", default=None, help="Filter by ticket id.")
@click.option(
    "--status",
    "-s",
    default=None,
    type=click.Choice([s.value for s in QueueItemStatus]),
    help="Filter by task status.",
)
@click.option("--client", "-c", default=None, help="Filter by client name.")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON array.")
@handle_errors
def dev_queue_tasks(
    ticket: str | None,
    status: str | None,
    client: str | None,
    output_json: bool,
) -> None:
    """List dev-queue tasks with typed field output.

    Programmatic inspection view. For the human aggregate summary use dev-queue status.
    """
    queue = load_dev_queue()
    tasks: list[TicketTask] = queue.tasks

    if ticket is not None:
        tasks = [t for t in tasks if t.ticket_id == ticket]

    if status is not None:
        target_status = QueueItemStatus(status)
        tasks = [t for t in tasks if t.status == target_status]

    if client is not None:
        tasks = [t for t in tasks if t.client == client]

    if output_json:
        click.echo(json.dumps([_task_to_dict(t) for t in tasks]))
    else:
        _print_tasks_human(tasks)


@dev_queue.command(name="refresh-all")
@handle_errors
def dev_queue_refresh_all() -> None:
    """Fast-forward main on every configured client repo.

    Runs ``git pull --ff-only origin <default_branch>`` for each client.
    Does NOT emit events — absence of ``ticket.needs_sync`` on the next
    dispatch tick confirms the refresh succeeded.
    """
    clients = load_clients()
    had_error = False
    for client in clients.values():
        try:
            before, after = fast_forward_main(client, ignore_untracked=True)
            if before == after:
                click.echo(f"{client.name}: already up to date ({before[:8]})")
            else:
                click.echo(f"{client.name}: updated {before[:8]}..{after[:8]}")
        except MissingWorkspaceError as exc:
            click.echo(f"{client.name}: SKIP — {exc}", err=True)
            # missing workspace is config-hygiene; does not contribute to had_error
        except WorktreeError as exc:
            click.echo(f"{client.name}: ERROR — {exc}", err=True)
            had_error = True
    if had_error:
        raise click.exceptions.Exit(1)
