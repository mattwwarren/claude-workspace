"""Orchestrator pipeline commands: status, retirement, dashboards, reap drain."""

from __future__ import annotations

import json
import logging
import time

import click

from cw.board import run_board
from cw.cli._base import _complete_client, _resolve_client, handle_errors, main
from cw.config import load_state
from cw.doctor import _reap_session_by_selector
from cw.events import advance_cursor, read_events
from cw.exceptions import CwError, LaneNotFoundError
from cw.models import (
    DEFAULT_LANE,
    OrchestratorEventType,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client
from cw.orchestrate import (
    MissingWorkerEntry,
    OrchestratorStatus,
    WorkerEntry,
    orchestrator_parent,
    orchestrator_status,
    orchestrator_workers,
    retire_merged_prs,
)
from cw.reconcile import ProposedAction
from cw.spawn import spawn_create_impl

logger = logging.getLogger(__name__)


@main.group()
def orchestrate() -> None:
    """Orchestrator pipeline: status snapshot and PR retirement.

    Driving a sprint? See `cw guide`.
    """


def _should_show_lane_breakdown(lanes: dict[str, dict[str, int]]) -> bool:
    """Return True when lane breakdown adds information beyond the top-level totals."""
    if not lanes:
        return False
    if len(lanes) > 1:
        return True
    return next(iter(lanes)) != DEFAULT_LANE


def _format_status_human(status: OrchestratorStatus) -> str:
    """Render an OrchestratorStatus as a human-readable string."""
    ts = status.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [f"Orchestrator status (as of {ts})", ""]

    lines.append(f"Pending tickets:   {len(status.pending_tickets)}")
    lines.extend(
        f"  - {t.ticket_id}  client={t.client}  priority={t.priority}"
        for t in status.pending_tickets
    )

    lines.extend(("", "Last dispatch tick:"))
    if status.last_tick_by_client:
        for client, tick in sorted(status.last_tick_by_client.items()):
            lines.append(
                f"  - {client}  claimed={tick.claimed}  pending={tick.pending}"
                f"  running={tick.running}/{tick.cap}"
                f"  skip={tick.skip_reason}"
                f"  at={tick.tick_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )
            if _should_show_lane_breakdown(tick.lanes):
                for lane_name, stats in sorted(tick.lanes.items()):
                    lines.append(
                        f"    {lane_name}: claimed={stats.get('claimed', 0)}"
                        f" running={stats.get('running', 0)}"
                        f" blocked={stats.get('blocked', 0)}"
                        f" pending={stats.get('pending', 0)}"
                    )
    else:
        lines.append("  (no dispatch ticks recorded)")

    lines.extend(("", f"Running sessions:  {len(status.running_sessions)}"))
    for s in status.running_sessions:
        line = f"  - {s.id}  {s.name}  status={s.status}"
        if s.last_stage:
            line += f"  last_stage={s.last_stage}"
        else:
            _stage_unknown = (
                "  last_stage=(unknown"
                " — global auto-dev.md not yet emitting stage events)"
            )
            line += _stage_unknown
        lines.append(line)

    lines.extend(("", f"Monitored PRs:     {len(status.monitored_prs)}"))
    for pr in status.monitored_prs:
        ci = pr.ci_status if pr.ci_status is not None else "(none)"
        mg = str(pr.mergeable) if pr.mergeable is not None else "(none)"
        lines.append(
            f"  - {pr.repo}#{pr.pr_number}  role={pr.role}  status={pr.status}"
            f"  unresolved={pr.unresolved_threads}  ci={ci}  mergeable={mg}"
        )

    lines.extend(("", f"Recent events:     {len(status.recent_events)}"))
    lines.extend(
        f"  - {e.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}  {e.id}  {e.type}"
        for e in status.recent_events
    )

    return "\n".join(lines)


@orchestrate.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@handle_errors
def orchestrate_status(as_json: bool) -> None:
    """Show a snapshot of the orchestrator subsystem.

    Includes pending dev-queue tickets, running sessions, PRs being
    monitored, and the last 20 orchestrator events.
    """
    snapshot = orchestrator_status()
    if as_json:
        click.echo(snapshot.model_dump_json(indent=2))
    else:
        click.echo(_format_status_human(snapshot))


@orchestrate.command(name="retire")
@handle_errors
def orchestrate_retire() -> None:
    """Run a single PR-retirement pass and print retired session IDs."""
    retired = retire_merged_prs()
    if not retired:
        click.echo("No sessions retired.")
        return
    click.echo(f"Retired {len(retired)} session(s):")
    for sid in retired:
        click.echo(f"  {sid}")


_ORCHESTRATE_WATCH_DEPRECATION = (
    "Note: `cw orchestrate watch` is deprecated and will be removed in the "
    "next release. Use `cw board` instead."
)


@orchestrate.command(name="watch")
@click.option(
    "--interval",
    type=int,
    default=2,
    show_default=True,
    help="Seconds between refreshes (1-60).",
)
@click.option(
    "--client",
    "client_filter",
    default=None,
    shell_complete=_complete_client,
    help="Only render this client.",
)
@handle_errors
def orchestrate_watch(
    interval: int,
    client_filter: str | None,
) -> None:
    """Render the lane x stage board live, refreshing on an interval.

    Repointed to the `cw board` render surface (issue #986). Press Ctrl-C
    to exit.

    Deprecated: will be removed in the next release; use `cw board` directly.
    """
    click.echo(_ORCHESTRATE_WATCH_DEPRECATION, err=True)
    run_board(interval=interval, client_filter=client_filter)


def _format_workers_human(
    present: list[WorkerEntry],
    missing: list[MissingWorkerEntry],
) -> str:
    """Render worker lists as a human-readable string."""

    def _present_line(w: WorkerEntry) -> str:
        branch = w.branch if w.branch is not None else "(none)"
        ts = w.last_activity.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"  {w.id}  status={w.status}  branch={branch}  last_activity={ts}"

    lines = [_present_line(w) for w in present]
    lines.extend(f"  {m.id}  status=missing" for m in missing)
    return "\n".join(lines)


@orchestrate.command(name="workers")
@click.argument("orchestrator_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@handle_errors
def orchestrate_workers(orchestrator_id: str, as_json: bool) -> None:
    """List worker sessions belonging to ORCHESTRATOR_ID.

    Shows id, status, branch, and last activity for each worker.
    Workers whose session records have been deleted are labelled 'missing'.
    Drift repair is handled by 'cw doctor'.
    """
    present, missing = orchestrator_workers(orchestrator_id)
    if as_json:
        present_dicts: list[dict[str, object]] = [
            {
                "id": w.id,
                "status": w.status,
                "branch": w.branch,
                "last_activity": w.last_activity.isoformat(),
            }
            for w in present
        ]
        missing_dicts: list[dict[str, object]] = [
            {"id": m.id, "missing": True} for m in missing
        ]
        click.echo(json.dumps(present_dicts + missing_dicts, indent=2))
    else:
        if not present and not missing:
            return
        click.echo(_format_workers_human(present, missing))


@orchestrate.command(name="parent")
@click.argument("worker_id")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@handle_errors
def orchestrate_parent(worker_id: str, as_json: bool) -> None:
    """Resolve WORKER_ID to its parent orchestrator session.

    Exits 0 with empty output (or null JSON) if the worker has no parent.
    Exits nonzero if the parent session ID is set but the record is missing
    from state (drift -- run 'cw doctor' to inspect).
    """
    entry = orchestrator_parent(worker_id)
    if entry is None:
        if as_json:
            click.echo("null")
        else:
            click.echo("no parent")
        return
    if as_json:
        data: dict[str, object] = {
            "id": entry.id,
            "status": entry.status,
            "surface_ref": entry.surface_ref,
        }
        click.echo(json.dumps(data, indent=2))
    else:
        surface = entry.surface_ref if entry.surface_ref is not None else "(none)"
        click.echo(f"{entry.id}  status={entry.status}  surface_ref={surface}")


@orchestrate.command(name="start")
@click.option(
    "--lane",
    "lane",
    required=True,
    help="Lane name to bind the ORCHESTRATE session to.",
)
@click.option(
    "--client",
    "client_name",
    default=None,
    help="Client name (defaults to first configured client).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@handle_errors
def orchestrate_start(lane: str, client_name: str | None, as_json: bool) -> None:
    """Spawn an ORCHESTRATE-purpose session bound to a lane.

    Records the lane-authority binding for use by Phase 4c.
    At most one live ORCHESTRATE session is allowed per (client, lane).
    """
    client_cfg = _resolve_client(client_name)
    declared = [ln.name for ln in client_cfg.effective_lanes]
    if lane not in declared:
        msg = (
            f"Lane '{lane}' is not declared for client '{client_cfg.name}'. "
            f"Declared lanes: {', '.join(declared)}. "
            f"Run: cw lane add {client_cfg.name} {lane}"
        )
        raise LaneNotFoundError(msg)

    # R5: Reject if a live ORCHESTRATE session already exists for (client, lane)
    state = load_state()
    live_statuses = {
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
        SessionStatus.BACKGROUNDED,
    }
    existing = next(
        (
            s
            for s in state.sessions
            if s.client == client_cfg.name
            and s.purpose == SessionPurpose.ORCHESTRATE
            and s.lane == lane
            and s.status in live_statuses
        ),
        None,
    )
    if existing is not None:
        msg = (
            f"An ORCHESTRATE session for lane '{lane}' already exists: "
            f"{existing.id!r} (status: {existing.status.value}). "
            f"Clear it first with: cw spawn complete {existing.id} --status completed"
        )
        raise CwError(msg)

    native_daemon = get_native_daemon_client()
    session_id = spawn_create_impl(
        client=client_cfg,
        worktree=client_cfg.workspace_path,
        prompt=(
            f"You are the lane-authority binding for lane '{lane}' on client "
            f"'{client_cfg.name}'. This session records the binding; the "
            "event-consumption loop is added separately (Phase 4c). "
            "You may end your turn now."
        ),
        label=f"orchestrate/{lane}",
        purpose=SessionPurpose.ORCHESTRATE,
        lane=lane,
        permission_mode="acceptEdits",
        native_daemon=native_daemon,
    )

    if as_json:
        click.echo(
            json.dumps(
                {"session_id": session_id, "lane": lane, "client": client_cfg.name}
            )
        )
    else:
        click.echo(f"Spawned orchestrate session for lane '{lane}': {session_id}")


_POLL_INTERVAL_SECONDS: float = 5.0


def _consumer_name(client: str, lane: str) -> str:
    """Return the event cursor consumer name for an orchestrate run consumer.

    Sanitizes path separators so events._cursor_path does not create nested
    directories under cursors/ when client or lane names contain slashes.
    """
    sanitized_client = client.replace("/", "-")
    sanitized_lane = lane.replace("/", "-")
    return f"orchestrate-{sanitized_client}-{sanitized_lane}"


def _drain_reap_proposals(client: str, lane: str) -> int:
    """Drain one pass of SESSION_REAP_PROPOSED events for the given lane.

    Authorizes REVERT_TASK and CRASH_COMPLETE by calling
    _reap_session_by_selector. Logs-and-leaves PARK_BLOCKED_ON_USER (already
    routed to operator review by reconcile).

    Returns the count of events processed (consumed from cursor).
    """
    consumer = _consumer_name(client, lane)
    events = read_events(
        consumer=consumer,
        event_types=[OrchestratorEventType.SESSION_REAP_PROPOSED],
    )
    # Load state once for the entire drain pass; sequential consumer, no
    # lock needed here — _reap_session_by_selector acquires sessions_lock()
    # itself when it mutates (cf. #387/#563 non-reentrancy constraint).
    state = load_state()
    processed = 0
    for event in events:
        payload = event.payload
        if payload.get("lane") != lane:
            # Advance cursor past events for other lanes so they are not
            # replayed on every drain, but do not count them as processed
            # by this consumer (lane isolation: each lane owns its events).
            advance_cursor(consumer, event.id)
            continue
        session_id = payload.get("session_id", "")
        proposed_action = payload.get("proposed_action", "")

        # Idempotency guard: skip already-terminal sessions.
        # Use {ACTIVE, IDLE, BACKGROUNDED} per spec R3 — terminal statuses
        # (COMPLETED, PENDING, etc.) trigger the skip; live sessions proceed.
        # _reap_session_by_selector's own lock guard is the authoritative
        # fence against double-reap (cf. #387/#563).
        session = next((s for s in state.sessions if s.id == session_id), None)
        if session is None or session.status not in {
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
            SessionStatus.BACKGROUNDED,
        }:
            logger.info(
                "orchestrate run: session %s already resolved, skipping", session_id
            )
            advance_cursor(consumer, event.id)
            processed += 1
            continue

        if proposed_action in {
            ProposedAction.REVERT_TASK.value,
            ProposedAction.CRASH_COMPLETE.value,
        }:
            # Why: _reap_session_by_selector acquires sessions_lock() itself and
            # is NOT reentrant. Safe here because orchestrate run is a standalone
            # command, NOT inside reconcile's held sessions_lock/_reconcile_locked
            # window. Never invoke this from inside a held lock (cf. #387/#563).
            # Why: under reap_policy=auto, sessions are already terminal when this
            # consumer reads the event; the status guard above makes it a no-op.
            _reap_session_by_selector(
                session_id,
                authority="orchestrate-run",
                lane=lane,
                proposed_action=proposed_action,
                correlation_id=event.id,
            )
            logger.info(
                "orchestrate run: authorized reap for session %s (action=%s)",
                session_id,
                proposed_action,
            )
        else:
            # PARK_BLOCKED_ON_USER or unknown action: leave at BLOCKED_ON_USER
            # routing for the operator. Salvage deferred to follow-on ticket.
            logger.info(
                "orchestrate run: action %s for session %s deferred"
                " (not authorize-eligible)",
                proposed_action,
                session_id,
            )

        advance_cursor(consumer, event.id)
        processed += 1
    return processed


@orchestrate.command(name="run")
@click.option(
    "--lane",
    "lane",
    required=True,
    help="Lane name to consume SESSION_REAP_PROPOSED events for.",
)
@click.option(
    "--client",
    "client_name",
    default=None,
    help="Client name (defaults to first configured client).",
)
@click.option(
    "--once",
    "once",
    is_flag=True,
    help="Drain available events once and exit (default: poll loop).",
)
@handle_errors
def orchestrate_run(lane: str, client_name: str | None, once: bool) -> None:
    """Consume SESSION_REAP_PROPOSED events for a lane and authorize reaps.

    Requires an ORCHESTRATE binding for the lane (created by
    ``cw orchestrate start --lane <lane>``). Authorizes clear-cut phantom
    reaps via the same path as ``cw doctor --reap``; defers salvage to a
    follow-on ticket.
    """
    client_cfg = _resolve_client(client_name)
    declared = [ln.name for ln in client_cfg.effective_lanes]
    if lane not in declared:
        msg = (
            f"Lane '{lane}' is not declared for client '{client_cfg.name}'. "
            f"Declared lanes: {', '.join(declared)}. "
            f"Run: cw lane add {client_cfg.name} {lane}"
        )
        raise LaneNotFoundError(msg)

    # Any-status match: orchestrate start's binding self-completes to COMPLETED.
    state = load_state()
    binding = next(
        (
            s
            for s in state.sessions
            if s.client == client_cfg.name
            and s.purpose == SessionPurpose.ORCHESTRATE
            and s.lane == lane
        ),
        None,
    )
    if binding is None:
        msg = (
            f"No ORCHESTRATE binding for lane '{lane}'; "
            f"run `cw orchestrate start --lane {lane}` first."
        )
        raise CwError(msg)

    if once:
        _drain_reap_proposals(client_cfg.name, lane)
        return

    try:
        while True:
            _drain_reap_proposals(client_cfg.name, lane)
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        click.echo("orchestrate run: stopped.", err=True)
        raise click.exceptions.Exit(130) from None


_ORCHESTRATOR_AGENT = "cw-orchestrator"
_ORCHESTRATOR_CHANNEL = "server:cw-pr-events"


@main.command(name="orchestrator-start")
@click.option("--name", default=_ORCHESTRATOR_AGENT, help="Session label.")
@click.option(
    "--client",
    default=None,
    help="Client to scope the orchestrator to. Defaults to first configured client.",
)
@handle_errors
def orchestrator_start(
    name: str,
    client: str | None,
    native_daemon: NativeDaemonClient | None = None,
) -> None:
    """Spawn a long-running cw orchestrator session driven by the cw-pr-events channel.

    The session listens for PR events emitted by cw daemon and reacts via
    the cw-orchestrator agent skill.
    """
    client_cfg = _resolve_client(client)
    extra_args = [
        "--agent",
        _ORCHESTRATOR_AGENT,
        "--dangerously-load-development-channels",
        _ORCHESTRATOR_CHANNEL,
    ]
    session_id = spawn_create_impl(
        client=client_cfg,
        worktree=client_cfg.workspace_path,
        prompt="You are the cw orchestrator session. Wait for channel events.",
        label=name,
        extra_args=extra_args,
        permission_mode="acceptEdits",
        native_daemon=native_daemon,
    )
    click.echo(f"Spawned orchestrator session: {session_id}")
