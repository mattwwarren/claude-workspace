"""Orchestrator development-queue commands (``dev-queue`` group)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import click

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.cli._base import _complete_client, handle_errors, main
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.config import get_client, load_clients, load_orchestrator_config, load_state
from cw.dev_queue import (
    _find_ticket,
    add_ticket,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    load_dev_queue,
    move_ticket,
    remove_ticket,
    resolve_client,
    wait_for_terminal,
)
from cw.dispatch import TICK_STALE_SECONDS, run_dispatch_loop
from cw.events import record_event
from cw.exceptions import CwError, MissingWorkspaceError, WorktreeError
from cw.models import (
    DEFAULT_LANE,
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
    _locate_session_transcript,
    resolve_idle_watchdog_budget,
)
from cw.session import _is_native_surface_ref
from cw.worktree import fast_forward_main


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
@handle_errors
def dev_queue_add(
    tickets: tuple[str, ...],
    client: str | None,
    priority: int,
    headless_timeout_override: int | None,
    scope_hint: str | None,
    lane_name: str,
) -> None:
    """Enqueue one or more tickets for dispatch."""
    config = load_orchestrator_config()
    for ticket_id in tickets:
        resolved = resolve_client(ticket_id, config, client)
        task = TicketTask(
            ticket_id=ticket_id,
            client=resolved,
            priority=priority,
            headless_timeout_override=headless_timeout_override,
            scope_hint=scope_hint,
            lane=lane_name,
        )
        inserted = add_ticket(task)
        if not inserted:
            click.echo(
                f"Skipped {ticket_id} -> {resolved}: already queued"
                " (pending or running).",
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


def _emit_dev_queue_lane_breakdown(tasks: list[TicketTask]) -> None:
    """Print indented lane lines for tasks when multi-lane or non-default lanes used.

    Groups tasks by lane and emits one line per lane showing pending, running,
    and blocked counts.  Skipped when all tasks share the single default lane.
    """
    # Collect lanes that are either non-default OR appear alongside other lanes
    lanes_seen: set[str] = {t.lane for t in tasks}
    if len(lanes_seen) <= 1 and lanes_seen == {DEFAULT_LANE}:
        return
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
        click.echo(
            f"    lane {lane_name}:"
            f" pending={pending} running={running} blocked={blocked}"
        )


@dev_queue.command(name="status")
@click.option("--client", "-c", default=None, help="Filter by client.")
@handle_errors
def dev_queue_status(client: str | None) -> None:
    """Show dev queue status grouped by client."""
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
        f"  {'COMPLETED':>9}  {'CANCELLED':>9}  TICKETS"
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
        ticket_ids = ", ".join(t.ticket_id for t in client_tasks)
        click.echo(
            f"{client_name:<20} {len(pending_tasks):>7}  {len(running_tasks):>7}"
            f"  {len(blocked_tasks):>7}  {len(completed_tasks):>9}"
            f"  {len(cancelled_tasks):>9}  {ticket_ids}"
        )

    tick_data = latest_tick_summary_by_client()
    if tick_data:
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
                    f"  running={tick.running}/{tick.cap}"
                    f"  skip={tick.skip_reason}"
                )
                age = int((now - tick.tick_at).total_seconds())
                if (now - tick.tick_at).total_seconds() > TICK_STALE_SECONDS:
                    tick_line += f" [STALE — no tick in {age}s]"
                click.echo(tick_line)
                _emit_dev_queue_lane_breakdown(by_client[client_name])


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


_PLAN_DEFAULT_TIMEOUT = 300

_WAIT_DEFAULT_TIMEOUT: int = 300
_WAIT_EXIT_FAILED: int = 1
_WAIT_EXIT_BLOCKED: int = 2
_WAIT_EXIT_ATTENTION: int = 3
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
    BLOCKED_ON_USER. COMPLETED returns normally; every other terminal status
    raises ``click.exceptions.Exit``.
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


def _transcript_age_seconds(
    session: Session,
    now: datetime,
) -> float | None:
    """Return seconds since the session's transcript was last written, or None.

    Returns None when the transcript file cannot be located.  Uses
    :func:`~cw.reconcile._locate_session_transcript` for precise per-session
    lookup (surface_ref-prefix glob, #541).
    """
    try:
        transcript = _locate_session_transcript(session)
        if transcript is None:
            return None
        mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
        return (now - mtime).total_seconds()
    except OSError:
        return None


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
    }


def _print_tasks_human(tasks: list[TicketTask]) -> None:
    if not tasks:
        click.echo("No tasks found.")
        return
    headers = ["TICKET_ID", "CLIENT", "STATUS", "SESSION_ID", "ATTEMPTS", "LANE"]
    col_widths = [12, 16, 16, 12, 8, 12]
    header = "  ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths, strict=True))
    click.echo(header)
    click.echo("-" * len(header))
    for t in tasks:
        row = [
            t.ticket_id[:12],
            t.client[:16],
            t.status.value[:16],
            (t.session_id or "-")[:12],
            str(t.attempts)[:8],
            t.lane[:12],
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
