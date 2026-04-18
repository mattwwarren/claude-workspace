"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import click
from click.shell_completion import CompletionItem

from cw import __version__
from cw.cmux import CmuxAdapter, get_cmux_adapter
from cw.config import (
    get_client,
    init_client,
    load_clients,
    load_orchestrator_config,
    load_state,
    save_state,
    show_config,
)
from cw.daemon import run_watcher_tick
from cw.dev_queue import add_ticket, list_tickets, resolve_client
from cw.dispatch import run_dispatch_loop
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    OrchestratorEventType,
    QueueItem,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
    TicketTask,
)
from cw.orchestrate import OrchestratorStatus, orchestrator_status, retire_merged_prs
from cw.plan import run_planner
from cw.queue import (
    add_item,
    claim_by_id,
    claim_next,
    clear_queue,
    complete_item,
    fail_item,
    load_queue,
    peek_next,
    remove_item,
)
from cw.session import (
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)
from cw.spawn import spawn_create_impl
from cw.wrapper import run_claude_wrapper, signal_idle


def handle_errors[F: Callable[..., object]](fn: F) -> F:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper  # type: ignore[return-value]


def _complete_client(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete client names from config."""
    return [
        CompletionItem(name) for name in load_clients() if name.startswith(incomplete)
    ]


def _complete_session(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete session names from backgrounded sessions."""
    state = load_state()
    return [
        CompletionItem(s.name)
        for s in state.sessions
        if s.name.startswith(incomplete) and s.status != SessionStatus.COMPLETED
    ]


@click.group()
@click.version_option(version=__version__, prog_name="cw")
def main() -> None:
    """Claude Workspace - multi-session orchestrator for Claude Code."""


@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default="impl",
    help="Session purpose.",
)
@click.option(
    "--worktree",
    "-w",
    default=None,
    help="Git branch for worktree isolation (e.g. feat/search).",
)
@handle_errors
def start(client: str, purpose: str, worktree: str | None) -> None:
    """Start or resume a Claude Code session for a client."""
    start_session(client, purpose, worktree=worktree)


@main.command()
@click.argument(
    "session_name", required=False, default=None, shell_complete=_complete_session
)
@click.option(
    "--notify",
    "-n",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Notify a sibling session after backgrounding.",
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Mark as auto-backgrounded (used by hooks).",
)
@click.option(
    "--all",
    "all_sessions",
    is_flag=True,
    default=False,
    help="Background all active sessions sequentially.",
)
@handle_errors
def bg(
    session_name: str | None,
    notify: str | None,
    auto: bool,
    all_sessions: bool,
) -> None:
    """Background the current session (auto-handoff).

    Optionally specify SESSION_NAME to background a specific session
    remotely (e.g. 'personal/debt' or a session ID).

    Use --all to background every active session sequentially.
    """
    if all_sessions:
        background_all_sessions(notify=notify, auto=auto)
    else:
        background_session(session_name, notify=notify, auto=auto)


@main.command()
@click.argument("session_name", shell_complete=_complete_session)
@handle_errors
def resume(session_name: str) -> None:
    """Resume a backgrounded session."""
    resume_session(session_name)


@main.command(name="list")
@handle_errors
def list_sessions() -> None:
    """List all sessions across clients."""
    _display_sessions()


@main.command()
@handle_errors
def status() -> None:
    """Show status dashboard across all clients."""
    _display_status()


@main.command()
@click.argument(
    "session_name", required=False, default=None, shell_complete=_complete_session
)
@click.option("--cleanup", is_flag=True, help="Remove associated worktree.")
@click.option("--force", is_flag=True, help="Force worktree removal.")
@handle_errors
def done(session_name: str | None, cleanup: bool, force: bool) -> None:
    """Mark a session as completed (not resumable).

    Optionally removes the associated worktree with --cleanup.
    """
    done_session(session_name, cleanup=cleanup, force=force)


@main.command()
@handle_errors
def config() -> None:
    """Show current configuration."""
    show_config()


@main.command(name="init")
@click.argument("name", required=False, default=None)
@click.option(
    "--path",
    "-p",
    type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="Path to the project repository.",
)
@click.option("--branch", "-b", default="main", help="Default branch name.")
@click.option(
    "--purposes",
    default=None,
    help="Comma-separated session purposes (e.g. impl,idea,debt).",
)
@handle_errors
def init(
    name: str | None,
    path: Path | None,
    branch: str,
    purposes: str | None,
) -> None:
    """Initialize a new client configuration.

    \b
    Non-interactive (scriptable):
      cw init my-project --path /path/to/repo
      cw init my-project --path /path/to/repo --branch develop

    \b
    Interactive (human-friendly):
      cw init
    """
    if name is None:
        # Interactive mode
        name = click.prompt("Client name")
        if path is None:
            path_str = click.prompt("Repository path", type=str)
            resolved = Path(path_str).resolve()
            if not resolved.is_dir():
                msg = f"Path does not exist or is not a directory: {resolved}"
                raise CwError(msg)
            path = resolved
        branch = click.prompt("Default branch", default=branch)

    if path is None:
        msg = (
            "Path is required: use --path or run without arguments for interactive mode"
        )
        raise CwError(msg)

    purpose_list = None
    if purposes:
        purpose_list = [p.strip() for p in purposes.split(",")]

    init_client(name, path, default_branch=branch, auto_purposes=purpose_list)

    click.echo(f"Added client '{name}' to configuration.")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  cw start {name}              # Start a session")
    click.echo("  cw config                    # View configuration")


def _relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string."""
    if dt is None:
        return "unknown"

    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m}m ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    d = seconds // 86400
    return f"{d}d ago"


def _display_sessions() -> None:
    """Display all tracked sessions."""
    state = load_state()

    if not state.sessions:
        click.echo("No sessions tracked.")
        return

    click.echo(f"{'CLIENT':<18} {'PURPOSE':<10} {'STATUS':<14} {'ID':<10} {'SINCE'}")
    click.echo("-" * 70)

    for s in state.sessions:
        if s.status == SessionStatus.COMPLETED:
            continue

        if s.status == SessionStatus.ACTIVE:
            since = _relative_time(s.resumed_at or s.started_at)
        elif s.status == SessionStatus.IDLE:
            since = _relative_time(s.idle_at or s.started_at)
        elif s.status == SessionStatus.BACKGROUNDED:
            since = _relative_time(s.backgrounded_at or s.started_at)
        else:
            since = _relative_time(s.started_at)

        click.echo(f"{s.client:<18} {s.purpose:<10} {s.status:<14} {s.id:<10} {since}")


def _check_and_mark_dead_sessions(_state: CwState) -> list[Session]:
    """Check active sessions for dead surfaces, mark them COMPLETED.

    Currently a stub — cmux health check will be implemented in a follow-up
    ticket once the cmux surface liveness API is determined.
    """
    return []


def _display_status() -> None:
    """Show a summary dashboard across all clients."""
    state = load_state()
    clients = load_clients()

    dead = _check_and_mark_dead_sessions(state)
    for s in dead:
        click.echo(f"Detected crashed session: {s.name} (crashed)")

    active = state.active_sessions()
    idled = state.idled_sessions()
    backgrounded = state.backgrounded_sessions()

    click.echo(f"Clients configured: {len(clients)}")
    click.echo(f"Active sessions:    {len(active)}")
    click.echo(f"Idle sessions:      {len(idled)}")
    click.echo(f"Backgrounded:       {len(backgrounded)}")
    click.echo()

    if active:
        click.echo("Active:")
        for s in active:
            since = _relative_time(s.resumed_at or s.started_at)
            click.echo(f"  {s.name} (since {since})")

    if idled:
        click.echo("Idle:")
        for s in idled:
            since = _relative_time(s.idle_at or s.started_at)
            click.echo(f"  {s.name} (since {since})")

    if backgrounded:
        click.echo("Backgrounded:")
        for s in backgrounded:
            handoff = (
                f" handoff: {s.last_handoff_path.name}" if s.last_handoff_path else ""
            )
            click.echo(f"  {s.name}{handoff}")


# --- Queue command group ---


@main.group()
def queue() -> None:
    """Manage the task queue."""


@queue.command(name="add")
@click.argument("client", shell_complete=_complete_client)
@click.argument("description")
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default="debt",
    help="Queue purpose.",
)
@click.option("--prompt", default=None, help="Exact prompt for Claude.")
@click.option("--priority", type=int, default=0, help="Priority (higher = sooner).")
@handle_errors
def queue_add(
    client: str,
    description: str,
    purpose: str,
    prompt: str | None,
    priority: int,
) -> None:
    """Add a work item to the queue."""
    task = TaskSpec(
        description=description,
        purpose=SessionPurpose(purpose),
        prompt=prompt or description,
        priority=priority,
    )
    item = add_item(client, task)
    click.echo(f"Added queue item: {item.id} ({description})")


@queue.command(name="list")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Filter by purpose.",
)
@click.option(
    "--status",
    "status_filter",
    type=click.Choice([e.value for e in QueueItemStatus]),
    default=None,
    help="Filter by status.",
)
@handle_errors
def queue_list(
    client: str,
    purpose: str | None,
    status_filter: str | None,
) -> None:
    """Show queue items for a client."""
    store = load_queue(client)
    items = store.items
    if purpose:
        items = [i for i in items if i.task.purpose == purpose]
    if status_filter:
        items = [i for i in items if i.status == status_filter]

    if not items:
        click.echo("Queue is empty.")
        return

    click.echo(f"{'ID':<10} {'STATUS':<12} {'PURPOSE':<10} {'DESCRIPTION'}")
    click.echo("-" * 60)
    for item in items:
        desc = item.task.description[:40]
        click.echo(f"{item.id:<10} {item.status:<12} {item.task.purpose:<10} {desc}")


@queue.command(name="remove")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@handle_errors
def queue_remove(client: str, item_id: str) -> None:
    """Remove an item from the queue."""
    remove_item(client, item_id)
    click.echo(f"Removed queue item: {item_id}")


@queue.command(name="clear")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Clear only items with this purpose.",
)
@click.option("--completed", is_flag=True, help="Clear only completed items.")
@handle_errors
def queue_clear(client: str, purpose: str | None, completed: bool) -> None:
    """Clear items from the queue."""
    purpose_enum = SessionPurpose(purpose) if purpose else None
    status_enum = QueueItemStatus.COMPLETED if completed else None
    removed = clear_queue(client, purpose=purpose_enum, status=status_enum)
    click.echo(f"Cleared {removed} item(s).")


@queue.command(name="next")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Filter by purpose.",
)
@click.option("--json", "as_json", is_flag=True, help="Output full QueueItem JSON.")
@handle_errors
def queue_next(client: str, purpose: str | None, as_json: bool) -> None:
    """Peek at the next pending item without claiming it."""
    purpose_enum = SessionPurpose(purpose) if purpose else None
    item = peek_next(client, purpose=purpose_enum)
    if item is None:
        click.echo("No pending items.")
        return
    if as_json:
        click.echo(item.model_dump_json(indent=2))
    else:
        click.echo(
            f"{item.id}  priority={item.task.priority}"
            f"  purpose={item.task.purpose}  {item.task.description}"
        )


@queue.command(name="claim")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Filter by purpose.",
)
@click.option("--id", "item_id", default=None, help="Claim a specific item by ID.")
@click.option("--json", "as_json", is_flag=True, help="Output full QueueItem JSON.")
@handle_errors
def queue_claim(
    client: str,
    purpose: str | None,
    item_id: str | None,
    as_json: bool,
) -> None:
    """Claim the next pending item (marks it RUNNING)."""
    item: QueueItem | None
    if item_id:
        item = claim_by_id(client, item_id)
    else:
        purpose_enum = SessionPurpose(purpose) if purpose else None
        item = claim_next(client, purpose=purpose_enum)
    if item is None:
        click.echo("No pending items to claim.")
        return
    if as_json:
        click.echo(item.model_dump_json(indent=2))
    else:
        click.echo(f"Claimed: {item.id} ({item.task.description})")


@queue.command(name="complete")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@click.option("--result", default="", help="Result summary text.")
@handle_errors
def queue_complete(client: str, item_id: str, result: str) -> None:
    """Mark a queue item as completed."""
    complete_item(client, item_id, result)
    click.echo(f"Completed: {item_id}")


@queue.command(name="fail")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@click.option("--error", "error_text", default="", help="Error description.")
@handle_errors
def queue_fail(client: str, item_id: str, error_text: str) -> None:
    """Mark a queue item as failed."""
    fail_item(client, item_id, error_text)
    click.echo(f"Failed: {item_id}")


# --- Event bus command group ---


@main.group()
def event() -> None:
    """Manage the orchestrator event bus."""


_VALID_EVENT_TYPES = {e.value for e in OrchestratorEventType}


@event.command(name="record")
@click.argument("event_type")
@click.option("--payload", default="{}", help="JSON payload string.")
@click.option("--correlation-id", default=None, help="Correlation ID to link events.")
@handle_errors
def event_record(
    event_type: str,
    payload: str,
    correlation_id: str | None,
) -> None:
    """Record an event to the inbox.

    EVENT_TYPE must be one of: ticket.enqueued, session.spawned,
    session.completed, pr.registered, pr.ci_failed,
    pr.review_received, pr.mergeable, pr.merged.
    """
    if event_type not in _VALID_EVENT_TYPES:
        valid = ", ".join(sorted(_VALID_EVENT_TYPES))
        msg = f"Unknown event type '{event_type}'. Valid types: {valid}"
        raise CwError(msg)

    try:
        payload_dict = json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON payload: {exc}"
        raise CwError(msg) from exc

    if not isinstance(payload_dict, dict):
        msg = "Payload must be a JSON object (dict), not a scalar or list."
        raise CwError(msg)

    etype = OrchestratorEventType(event_type)
    recorded = record_event(
        etype,
        payload_dict,
        correlation_id=correlation_id,
    )
    click.echo(f"Recorded event: {recorded.id} ({recorded.type})")


@event.command(name="tail")
@click.option(
    "--since",
    default=None,
    help="Consumer name (cursor) or ISO timestamp (e.g. 2025-01-01T00:00:00Z).",
)
@click.option(
    "--type",
    "type_filter",
    multiple=True,
    help="Filter by event type (repeatable).",
)
@click.option("--json", "as_json", is_flag=True, help="Output full event JSON.")
@handle_errors
def event_tail(
    since: str | None,
    type_filter: tuple[str, ...],
    as_json: bool,
) -> None:
    """Read events from the inbox.

    --since may be a consumer name (alphanumeric, e.g. 'daemon') whose
    persisted cursor determines the starting position, or an ISO 8601
    timestamp to filter by creation time.

    When a consumer name is given, the cursor advances automatically
    after reading.
    """
    # Determine if `since` is a consumer name or a timestamp.
    # Consumer names: alphanumeric + underscores (no colons, no dashes, no dots).
    consumer: str | None = None
    since_ts: datetime | None = None

    if since is not None:
        # Heuristic: consumer names are simple identifiers (no colons or dashes)
        if since.replace("_", "").isalnum():
            consumer = since
        else:
            try:
                since_ts = datetime.fromisoformat(since)
                if since_ts.tzinfo is None:
                    since_ts = since_ts.replace(tzinfo=UTC)
            except ValueError as exc:
                msg = (
                    f"Cannot parse --since value '{since}'"
                    " as consumer name or ISO timestamp."
                )
                raise CwError(msg) from exc

    # Resolve event type filters
    etype_filter: list[OrchestratorEventType] | None = None
    if type_filter:
        invalid = [t for t in type_filter if t not in _VALID_EVENT_TYPES]
        if invalid:
            valid = ", ".join(sorted(_VALID_EVENT_TYPES))
            msg = f"Unknown event type(s): {', '.join(invalid)}. Valid: {valid}"
            raise CwError(msg)
        etype_filter = [OrchestratorEventType(t) for t in type_filter]

    events = read_events(
        consumer=consumer,
        since_ts=since_ts,
        event_types=etype_filter,
    )

    if not events:
        click.echo("No events.")
        return

    for ev in events:
        if as_json:
            click.echo(ev.model_dump_json())
        else:
            ts = ev.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            corr = f" corr={ev.correlation_id}" if ev.correlation_id else ""
            click.echo(f"{ts}  {ev.id}  {ev.type}{corr}  {ev.payload}")

    # Advance consumer cursor to last event seen
    if consumer is not None and events:
        advance_cursor(consumer, events[-1].id)
        click.echo(f"Cursor advanced to: {events[-1].id}", err=True)


@main.command(name="run-claude")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@handle_errors
def run_claude(extra_args: tuple[str, ...]) -> None:
    """Wrapper around Claude that signals IDLE on exit.

    Used as the pane command in Zellij layouts. After Claude exits,
    transitions the session to IDLE and waits for daemon triggers.

    \b
    Examples:
      cw run-claude -- --resume
      cw run-claude -- --resume --append-system-prompt "..."
    """
    run_claude_wrapper(extra_args)


@main.command(name="pane-exited")
@click.option("--client", "-c", required=True, help="Client name.")
@click.option("--purpose", "-p", required=True, help="Session purpose.")
@click.option("--exit-code", type=int, default=0, help="Claude exit code.")
@handle_errors
def pane_exited(client: str, purpose: str, exit_code: int) -> None:
    """Explicitly signal that Claude exited in a pane.

    Fallback for cases where the wrapper isn't running. Transitions
    the session to IDLE.
    """
    signal_idle(client, purpose, exit_code=exit_code)
    click.echo(f"Signaled IDLE for {client}/{purpose} (exit code {exit_code}).")


@main.command(name="daemon")
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run one tick and exit (useful for testing or cron).",
)
@handle_errors
def daemon(once: bool) -> None:
    """Run the PR event watcher daemon.

    Polls review-monitor state files and emits orchestrator events
    for PR lifecycle changes (merged, CI failed, review received, mergeable).

    \b
    Run continuously (default):
      cw daemon

    \b
    Single tick (e.g. from cron):
      cw daemon --once
    """
    run_watcher_tick(once=once)


_COMPLETION_SCRIPTS = {
    "bash": 'eval "$(_CW_COMPLETE=bash_source cw)"',
    "zsh": 'eval "$(_CW_COMPLETE=zsh_source cw)"',
    "fish": "_CW_COMPLETE=fish_source cw | source",
}


@main.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Output shell completion activation script.

    Add to your shell profile:

    \b
      # Bash (~/.bashrc)
      eval "$(_CW_COMPLETE=bash_source cw)"

    \b
      # Zsh (~/.zshrc)
      eval "$(_CW_COMPLETE=zsh_source cw)"

    \b
      # Fish (~/.config/fish/config.fish)
      _CW_COMPLETE=fish_source cw | source
    """
    # Output the activation one-liner for the user to add to their profile
    click.echo("# Add this to your shell profile:")
    click.echo(_COMPLETION_SCRIPTS[shell])


# --- Dev-queue command group ---


@main.group(name="dev-queue")
def dev_queue() -> None:
    """Manage the orchestrator development queue."""


@dev_queue.command(name="add")
@click.argument("tickets", nargs=-1, required=True)
@click.option("--client", "-c", default=None, help="Target client name.")
@click.option("--priority", "-p", type=int, default=0, help="Priority (higher=sooner).")
@handle_errors
def dev_queue_add(tickets: tuple[str, ...], client: str | None, priority: int) -> None:
    """Enqueue one or more tickets for dispatch."""
    config = load_orchestrator_config()
    for ticket_id in tickets:
        resolved = resolve_client(ticket_id, config, client)
        task = TicketTask(ticket_id=ticket_id, client=resolved, priority=priority)
        add_ticket(task)
        record_event(
            OrchestratorEventType.TICKET_ENQUEUED,
            {"ticket_id": ticket_id, "client": resolved, "priority": priority},
        )
        click.echo(f"Enqueued {ticket_id} -> {resolved} (priority={priority})")


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

    header = f"{'CLIENT':<20} {'PENDING':>7}  {'RUNNING':>7}  {'COMPLETED':>9}  TICKETS"
    click.echo(header)
    click.echo("-" * 70)
    for client_name in clients_seen:
        client_tasks = by_client[client_name]
        pending_tasks = [t for t in client_tasks if t.status == QueueItemStatus.PENDING]
        running_tasks = [t for t in client_tasks if t.status == QueueItemStatus.RUNNING]
        completed_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.COMPLETED
        ]
        ticket_ids = ", ".join(t.ticket_id for t in client_tasks)
        click.echo(
            f"{client_name:<20} {len(pending_tasks):>7}  {len(running_tasks):>7}"
            f"  {len(completed_tasks):>9}  {ticket_ids}"
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
@handle_errors
def dev_queue_run(max_parallel: int | None, once: bool, use_plan: bool) -> None:
    """Run the dispatch loop, spawning sessions for pending tickets."""
    run_dispatch_loop(max_parallel=max_parallel, once=once, use_plan=use_plan)


_PLAN_DEFAULT_TIMEOUT = 300


def _run_plan_impl(
    *,
    client_name: str,
    timeout: int,
    adapter: CmuxAdapter,
    client_filter: str | None,
) -> int:
    """Spawn the planner, persist the result, and report status.

    Separated from the Click command so tests can inject the cmux adapter
    directly.  Returns 0 on success, 1 on validation/timeout failure.
    """
    client_config = get_client(client_name)
    result = run_planner(
        client=client_config,
        adapter=adapter,
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
    help="Client whose cmux workspace will host the planner session.",
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
    adapter = get_cmux_adapter()
    exit_code = _run_plan_impl(
        client_name=client,
        timeout=timeout,
        adapter=adapter,
        client_filter=filter_client,
    )
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)


# --- Orchestrate command group ---


@main.group()
def orchestrate() -> None:
    """Orchestrator pipeline: status snapshot and PR retirement."""


def _format_status_human(status: OrchestratorStatus) -> str:
    """Render an OrchestratorStatus as a human-readable string."""
    ts = status.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [f"Orchestrator status (as of {ts})", ""]

    lines.append(f"Pending tickets:   {len(status.pending_tickets)}")
    lines.extend(
        f"  - {t.ticket_id}  client={t.client}  priority={t.priority}"
        for t in status.pending_tickets
    )

    lines.append("")
    lines.append(f"Running sessions:  {len(status.running_sessions)}")
    lines.extend(
        f"  - {s.id}  {s.name}  status={s.status}" for s in status.running_sessions
    )

    lines.append("")
    lines.append(f"Monitored PRs:     {len(status.monitored_prs)}")
    lines.extend(
        f"  - {pr.repo}#{pr.pr_number}  status={pr.status}"
        f"  unresolved={pr.unresolved_threads}"
        for pr in status.monitored_prs
    )

    lines.append("")
    lines.append(f"Recent events:     {len(status.recent_events)}")
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
    adapter = get_cmux_adapter()
    retired = retire_merged_prs(adapter=adapter)
    if not retired:
        click.echo("No sessions retired.")
        return
    click.echo(f"Retired {len(retired)} session(s):")
    for sid in retired:
        click.echo(f"  {sid}")


# --- Spawn command group ---


def _spawn_create_impl(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt_file: Path,
    surface: str,
    label: str | None,
    adapter: CmuxAdapter,
) -> str:
    """Create a daemon-spawned session.

    Separated from the Click command so tests can inject adapters directly.
    Returns the new session's ID.

    Delegates to :func:`cw.spawn.spawn_create_impl`.
    """
    return spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt_file=prompt_file,
        surface=surface,
        label=label,
        adapter=adapter,
    )


def _spawn_close_impl(
    *,
    session_id: str,
    adapter: CmuxAdapter,
) -> None:
    """Close a daemon-spawned session.

    Separated from the Click command so tests can inject adapters directly.
    """
    state = load_state()
    sess = state.find_by_name_or_id(session_id)
    if sess is None:
        msg = f"Session '{session_id}' not found."
        raise CwError(msg)
    if sess.status == SessionStatus.COMPLETED:
        msg = f"Session '{session_id}' is already completed."
        raise CwError(msg)

    if sess.surface_ref is not None:
        adapter.close(sess.surface_ref)

    sess.status = SessionStatus.COMPLETED
    sess.completed_at = datetime.now(UTC)
    sess.completed_reason = CompletionReason.USER
    save_state(state)


@main.group(invoke_without_command=True)
@click.option("--client", "-c", default=None, help="Client name.")
@click.option("--worktree", "-w", default=None, help="Worktree path.")
@click.option("--prompt-file", "-f", default=None, help="Path to prompt file.")
@click.option(
    "--surface",
    "-s",
    default="split",
    type=click.Choice(["split", "tab"]),
    help="Surface type.",
)
@click.option("--label", "-l", default=None, help="Session label (default: daemon).")
@click.pass_context
@handle_errors
def spawn(
    ctx: click.Context,
    client: str | None,
    worktree: str | None,
    prompt_file: str | None,
    surface: str,
    label: str | None,
) -> None:
    """Spawn a daemon-managed Claude session or manage spawned sessions.

    When called directly (not via a subcommand), spawns a new session:

    \b
      cw spawn --client my-client --worktree /path/to/worktree --prompt-file prompt.txt

    Subcommands:
      close  Close a spawned session by session ID.
    """
    if ctx.invoked_subcommand is not None:
        return

    # Invoked as `cw spawn --client ...` (top-level create)
    missing: list[str] = []
    if client is None:
        missing.append("--client")
    if worktree is None:
        missing.append("--worktree")
    if prompt_file is None:
        missing.append("--prompt-file")
    if missing:
        opts = ", ".join(missing)
        msg = f"Missing required option(s): {opts}"
        raise CwError(msg)

    # At this point client/worktree/prompt_file are guaranteed non-None
    # (the `if missing` guard above raised CwError if any were absent).
    client_config = get_client(cast("str", client))
    adapter = get_cmux_adapter()
    session_id = _spawn_create_impl(
        client=client_config,
        worktree=Path(cast("str", worktree)),
        prompt_file=Path(cast("str", prompt_file)),
        surface=surface,
        label=label,
        adapter=adapter,
    )
    click.echo(session_id)


@spawn.command(name="close")
@click.argument("session_id")
@handle_errors
def spawn_close(session_id: str) -> None:
    """Close a spawned session by session ID.

    Calls adapter.close on the associated surface and marks the session
    as COMPLETED.

    \b
    Example:
      cw spawn close abc12345
    """
    # Validate session exists before acquiring the adapter (adapter may fail on
    # non-macOS when cmux is not installed).
    state = load_state()
    sess = state.find_by_name_or_id(session_id)
    if sess is None:
        not_found_msg = f"Session '{session_id}' not found."
        raise CwError(not_found_msg)
    if sess.status == SessionStatus.COMPLETED:
        already_done_msg = f"Session '{session_id}' is already completed."
        raise CwError(already_done_msg)

    adapter = get_cmux_adapter()
    _spawn_close_impl(session_id=session_id, adapter=adapter)
    click.echo(f"Closed session: {session_id}")
