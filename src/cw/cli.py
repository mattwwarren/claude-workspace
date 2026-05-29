"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import functools
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

import click
from click.shell_completion import CompletionItem

from cw import __version__
from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    AutoDevResult,
    BlockedResult,
    Status,
    extract_block,
    parse_stdout,
)
from cw.cmux import MultiplexerAdapter, get_cmux_adapter
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
from cw.dev_queue import (
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    dev_queue_lock,
    list_tickets,
    load_dev_queue,
    remove_ticket,
    resolve_client,
    save_dev_queue,
)
from cw.dispatch import _DISPATCH_CONSUMER, _apply_events_to_store, run_dispatch_loop
from cw.doctor import format_report, format_report_json, run_doctor
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import CwError, MissingWorkspaceError, WorktreeError
from cw.models import (
    ClientConfig,
    CompletionReason,
    OrchestratorEventType,
    QueueItem,
    QueueItemStatus,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
    TicketTask,
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
from cw.reconcile import reconcile, resolve_headless_budget, ticket_id_for_session
from cw.session import (
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)
from cw.spawn import spawn_create_impl
from cw.tui import DetailLevel, watch_flat
from cw.tui import watch as tui_watch
from cw.worktree import fast_forward_main
from cw.wrapper import run_claude_wrapper, signal_idle

if TYPE_CHECKING:
    from collections.abc import Callable


def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper


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
@click.option(
    "--parent",
    default=None,
    help="Parent session ID to link as a worker session.",
)
@handle_errors
def start(client: str, purpose: str, worktree: str | None, parent: str | None) -> None:
    """Start or resume a Claude Code session for a client."""
    start_session(client, purpose, worktree=worktree, parent=parent)


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
@click.option(
    "--interval",
    type=int,
    default=5,
    show_default=True,
    help="Seconds between data refreshes (1-60).",
)
@handle_errors
def watch(interval: int) -> None:
    """Live flat-table work board (sessions + dev-queue tickets).

    Keybindings: j/k navigate  p=peek  c=spawn-complete  o=open  r=refresh  q=quit
    """
    watch_flat(interval=interval, home=str(Path.home()))


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


@main.command()
@click.option(
    "--reap",
    is_flag=True,
    help="Also reconcile state with the live multiplexer and reap phantoms.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output report as JSON.",
)
@handle_errors
def doctor(reap: bool, as_json: bool) -> None:
    """Run environment preflight checks and print a health report.

    Reports the resolved backend, backend binary/daemon availability,
    config file locations and validity, and state file parseability.
    Exits non-zero if any check fails.

    With ``--reap``, also detects and repairs the following wedge conditions:

    \b
    Class 1 — wedge/pane-idle-but-active
      Session pane shows an idle shell with no recent worktree activity.
      Action: mark session COMPLETED, close pane, revert queue task to PENDING.
      Recipe: cw doctor --reap

    \b
    Class 2 — wedge/task-running-no-session
      Queue task is RUNNING but has no associated live session.
      Action: revert queue task to PENDING.
      Recipe: cw doctor --reap

    \b
    Class 3 — wedge/task-running-completed-session
      Queue task is RUNNING but its session is already COMPLETED.
      Action: revert queue task to PENDING.
      Recipe: cw doctor --reap

    \b
    Class 4 — wedge/repo-ahead-of-queue (advisory only)
      Branch is pushed to remote but queue task is still RUNNING.
      No automatic mutation — inspect and resolve manually.
      Recipe: cw spawn-complete <ticket_id> [--status shipped]

    ``--reap`` also reconciles session state with the live multiplexer,
    marking phantom sessions COMPLETED and reverting their tickets to PENDING.
    """
    report = run_doctor(reap=reap)
    if as_json:
        click.echo(format_report_json(report))
        raise click.exceptions.Exit(0 if report.ok else 1)
    click.echo(format_report(report))
    if not report.ok:
        raise click.exceptions.Exit(1)


@main.command(name="upgrade-workers")
@handle_errors
def upgrade_workers() -> None:
    """Restart all daemon-managed background sessions via ``claude respawn --all``.

    Run after upgrading cw or the Claude CLI so background workers pick up
    the current Claude binary. Wraps ``claude respawn --all`` (RFC 0001 Row 7);
    closes the gap previously filled by hand-rolled daemon-restart logic.

    Propagates the subprocess exit code. Surfaces stdout to the user; on
    non-zero exit also surfaces stderr.
    """
    try:
        result = subprocess.run(
            ["claude", "respawn", "--all"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        msg = (
            "claude binary not found on PATH. Install Claude Code, "
            "then re-run 'cw upgrade-workers'."
        )
        raise click.ClickException(msg) from e
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.returncode != 0:
        if result.stderr:
            click.echo(result.stderr, nl=False, err=True)
        raise click.exceptions.Exit(result.returncode)


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

    dead = _check_and_mark_dead_sessions()
    for name in dead:
        click.echo(f"Reaped phantom session: {name}")
    if dead:
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


def _check_and_mark_dead_sessions() -> list[str]:
    """Reconcile state with the native daemon and return reaped session names.

    Cheap passive reconciliation: called from every read path (``cw status``,
    ``cw list``, ``cw start``). The reconciler is idempotent and returns an
    empty list when nothing changed. :func:`cw.reconcile.reconcile` refuses
    to mass-reap when the daemon is unreachable or the roster is empty while
    active sessions still have surface refs (outage guard), so this helper
    is safe to run on every read path.
    """
    report = reconcile()
    return list(report.phantom_session_names)


def _display_status() -> None:
    """Show a summary dashboard across all clients."""
    state = load_state()
    clients = load_clients()

    dead = _check_and_mark_dead_sessions()
    for name in dead:
        click.echo(f"Reaped phantom session: {name}")
    if dead:
        # State mutated, reload so active/backgrounded lists reflect truth.
        state = load_state()

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
    session.completed, session.timed_out, stage.entered, stage.errored,
    pr.registered, pr.ci_failed, pr.review_received, pr.mergeable,
    pr.merged.
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

    Used as the pane command in the multiplexer workspace. After Claude
    exits, transitions the session to IDLE and waits for daemon triggers.

    \b
    Examples:
      cw run-claude -- --resume
      cw run-claude -- --resume --append-system-prompt "..."
    """
    run_claude_wrapper(extra_args)


# Max dispatch attempts before a validation_failed sentinel caps the task as
# FAILED rather than reverting to PENDING. See issue #251 Bug B.
_VALIDATION_FAILED_MAX_ATTEMPTS = 3

# AutoDevResult statuses that represent terminal outcomes the dev-queue should
# never auto-retry. Marking these COMPLETED prevents the race described in
# issue #251 Bug A where revert_completed_silent_tasks reverts a task to
# PENDING before consume_completed_sessions can process the SESSION_COMPLETED
# event — causing no_op and similar outcomes to trigger infinite re-dispatch.
# Also covers ambiguities_pending_resolution and premises_pending_verification
# (issue #316): these are terminal-pending-user, not retry candidates.
_TERMINAL_NO_RETRY_STATUSES: frozenset[str] = (
    frozenset(
        {
            "shipped",
            "no_op",
            "plan_pending_approval",
            "review_pending_approval",
            "merge_gate_blocked",
            "scope_exceeded",
            "forbidden_area",
        }
    )
    | PAUSED_FOR_USER_INPUT_STATUSES
)


def _apply_sentinel_to_task(
    ticket_id: str,
    cw_session_id: str,
    sentinel: AutoDevResult | BlockedResult,
) -> None:
    """Directly update the matching dev-queue task based on the sentinel result.

    Called from signal_stop *before* the session is marked COMPLETED so the
    task is already in its terminal state when revert_completed_silent_tasks
    runs. This closes the race window where:

      1. signal_stop marks session COMPLETED (save_state)
      2. dispatch loop reconcile fires: sees COMPLETED session + RUNNING task
         → reverts task to PENDING
      3. consume_completed_sessions reads the SESSION_COMPLETED event but
         finds task is PENDING (not RUNNING) → skips it
      4. next tick re-dispatches the ticket (infinite loop for no_op)

    See GitHub issue #251.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        target: TicketTask | None = None
        for task in store.tasks:
            if (
                task.ticket_id == ticket_id
                and task.session_id == cw_session_id
                and task.status == QueueItemStatus.RUNNING
            ):
                target = task
                break
        if target is None:
            return

        if isinstance(sentinel, AutoDevResult):
            if sentinel.status in _TERMINAL_NO_RETRY_STATUSES:
                target.status = QueueItemStatus.COMPLETED
            elif sentinel.status == "blocked":
                retry = (
                    sentinel.blocker is not None
                    and sentinel.blocker.retry_eligible is True
                )
                if retry:
                    target.status = QueueItemStatus.PENDING
                    target.session_id = None
                else:
                    target.status = QueueItemStatus.COMPLETED
            else:
                target.status = QueueItemStatus.COMPLETED
        else:
            # BlockedResult: sentinel failed to parse or was malformed.
            if sentinel.blocker.reason == "validation_failed":
                if target.attempts >= _VALIDATION_FAILED_MAX_ATTEMPTS:
                    target.status = QueueItemStatus.FAILED
                else:
                    target.status = QueueItemStatus.PENDING
                    target.session_id = None
            else:
                # Other parse failures (no_result_emitted, etc.) → retry.
                target.status = QueueItemStatus.PENDING
                target.session_id = None

        save_dev_queue(store)


def _parse_sentinel_from_transcript(
    cwd: str,
    claude_session_id: str | None,
) -> AutoDevResult | BlockedResult | None:
    """Return the parsed sentinel from the transcript, or None if absent.

    Claude stores session transcripts at:
      ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``

    where the encoded path replaces both ``/`` and ``.`` with ``-``. The JSONL
    contains one event per line; ``assistant`` events carry ``message.content``
    blocks whose ``text`` fields hold the model output, JSON-escaped (real
    newlines become the two-character sequence ``\\n``). Running ``extract_block``
    against the raw file therefore misses sentinels that are valid in their
    decoded form, so this scans each assistant text block individually after
    JSON decoding. Returns None on any I/O error or when no complete sentinel
    pair is found — distinct from a BlockedResult, which means the sentinel
    framing was present but the inner payload was unusable (§6 failure modes).

    Used by ``signal_stop`` for headless DAEMON sessions, whose result must
    be captured here because they bypass the cw wrapper entirely. See GitHub
    issue #225 (capture gap) and issue #176 Layer 1 (transcript-walk origin).
    """
    if not claude_session_id:
        return None
    encoded = cwd.replace("/", "-").replace(".", "-")
    transcript_path = (
        Path.home() / ".claude" / "projects" / encoded / f"{claude_session_id}.jsonl"
    )
    if not transcript_path.is_file():
        return None
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and extract_block(text) is not None:
                        return parse_stdout(text)
    except OSError:
        return None
    return None


def _sentinel_present_in_transcript(
    cwd: str,
    claude_session_id: str | None,
) -> bool:
    """Return True if the AUTO_DEV_RESULT sentinel block appears in the transcript.

    Thin wrapper around :func:`_parse_sentinel_from_transcript` preserved for
    callers that only need the boolean (Layer 1 budget gate in signal_stop).
    A non-None return — including a BlockedResult for malformed payloads —
    means the agent emitted *something*; the result-capture path uses the
    full parsed value, but the budget path only cares "did it emit?"
    """
    return _parse_sentinel_from_transcript(cwd, claude_session_id) is not None


@main.command(name="signal-stop")
@handle_errors
def signal_stop() -> None:
    """Emit SESSION_COMPLETED on a Stop hook fire.

    Reads the hook JSON from stdin, extracts ``cwd`` (the worktree path),
    reads ``<cwd>/.claude/cw-context.json`` for cw correlation IDs,
    transitions the matching Session to COMPLETED, and posts a
    ``session.completed`` event to the inbox so the dispatch consumer
    can transition the matching TicketTask to COMPLETED.

    Wired in via ``.claude/settings.local.json`` written by spawn into
    each dispatched session's worktree. Bypasses env-var loss under
    ``claude --bg`` (see GitHub issue #133, design in #147).

    Idempotent: a session already COMPLETED is a no-op (re-firing the
    Stop hook on a subsequent turn won't double-record).

    Best-effort: a missing or unreadable context file is a silent no-op
    so hook execution never blocks claude from exiting.

    Defers when the hook payload carries a non-empty ``background_tasks``
    list: the Stop hook fires at every main-agent turn boundary, and
    dispatching a ``run_in_background: true`` subagent ends the parent's
    turn while the subagent is still running. Completing the session
    here would orphan the subagent. See issue #151.
    """
    try:
        stdin_text = sys.stdin.read()
    except (OSError, ValueError):
        return
    if not stdin_text:
        return
    try:
        hook_payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return
    cwd_value = hook_payload.get("cwd") if isinstance(hook_payload, dict) else None
    if not isinstance(cwd_value, str):
        return
    context_path = Path(cwd_value) / ".claude" / "cw-context.json"
    if not context_path.is_file():
        return
    try:
        context = json.loads(context_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(context, dict):
        return

    cw_session_id = context.get("session_id")
    if not isinstance(cw_session_id, str):
        return

    bg_tasks = hook_payload.get("background_tasks")
    if isinstance(bg_tasks, list) and bg_tasks:
        # Turn boundary with pending background work — leave the session
        # in its current status; another Stop hook will fire when the bg
        # work drains (the contract `claude --bg + run_in_background: true`
        # relies on: the subagent's result arrives as the next main-agent
        # turn, which then ends, firing Stop again with background_tasks
        # empty). Without this guard, dispatching a run_in_background: true
        # subagent causes the parent to be marked COMPLETED and, for
        # DAEMON-origin sessions, killed via `claude stop`, orphaning the
        # in-flight subagent. See issue #151.
        #
        # Fast path: no state I/O. The idempotency guard below is
        # unreachable on this path by design — deferral leaves state
        # untouched regardless of current session status.
        #
        # Backstop: if the second Stop hook ever fails to fire (daemon
        # bug, subagent hard crash with no clean Stop), reconcile.py
        # eventually detects the phantom and marks the session CRASHED,
        # reverting any matching dev_queue task to PENDING for retry.
        # Recovery, not silent wedge.
        return

    state = load_state()
    session = next((s for s in state.sessions if s.id == cw_session_id), None)
    if session is None or session.status in (
        SessionStatus.COMPLETED,
        SessionStatus.IDLE,
        SessionStatus.TIMED_OUT,
    ):
        return

    claude_session_id = hook_payload.get("session_id")

    # Issue #285: stale-hook guard. When dispatch reuses a worktree for a
    # blocked→retry sequence, spawn_create_impl overwrites cw-context.json with
    # the new session's ID *before* the old Claude process finishes. The old
    # process can then fire one final Stop hook: the hook reads the new session's
    # CW ID from context but carries the old Claude UUID in its payload. Without
    # this guard the stale hook would parse the old (blocked) transcript and
    # apply that sentinel to the new session's task, reverting it to PENDING.
    # Fix: drop any DAEMON-origin hook whose Claude UUID doesn't match this
    # session's surface_ref (the 8-char prefix stored at spawn time).
    # USER-origin sessions are interactive and never have cw-context.json
    # overwritten by dispatch, so the guard does not apply to them.
    if (
        session.origin is SessionOrigin.DAEMON
        and isinstance(claude_session_id, str)
        and session.surface_ref is not None
        and not claude_session_id.startswith(session.surface_ref)
    ):
        return

    # Issue #165 Phase B: USER-origin sessions are interactive — the Stop
    # hook fires at every agent turn but the human is still driving. Mark
    # IDLE so wait loops / daemon triggers can react, but do NOT emit
    # SESSION_COMPLETED (no dev_queue task to retire) and do NOT call
    # native_daemon.stop (no roster entry to clean up). DAEMON-origin
    # falls through to the existing COMPLETED transition below.
    if session.origin is SessionOrigin.USER:
        if session.status != SessionStatus.ACTIVE:
            # BACKGROUNDED (or any non-ACTIVE state) — silent no-op so a
            # Stop hook firing on a session the user has explicitly
            # parked doesn't flip its status.
            return
        session.status = SessionStatus.IDLE
        if isinstance(claude_session_id, str):
            session.claude_session_id = claude_session_id
        save_state(state)
        return

    # Issue #176 Layer 1: headless backstop.
    #
    # A headless DAEMON session (ticket_id present in context) must NOT be
    # silently marked COMPLETED unless it emitted an AUTO_DEV_RESULT sentinel.
    # The bg_tasks guard above correctly defers when a subagent is in flight,
    # but the parent's *next* turn may end (with background_tasks=[]) before
    # it has finished its post-wait pipeline work — a silent orphan.
    #
    # Detection: DAEMON-origin + non-None ticket_id in context ≡ headless.
    # Sentinel check: look for the sentinel open tag in the Claude transcript.
    # Budget: if no sentinel AND wall-clock since session.started_at exceeds
    # HEADLESS_TIMEOUT_SECONDS, transition to TIMED_OUT (retry-eligible) so
    # the failure is loud and dev-queue can retry. Under budget: defer (return)
    # so another Stop hook (or reconcile) can catch it later.
    #
    # The guard does NOT replace the bg_tasks deferral — both fire independently.
    ticket_id_value = context.get("ticket_id")
    # ``headless: true`` in cw-context.json is written by spawn_create_impl
    # when dispatch launches a /auto-dev session. Absent (or False) for legacy
    # sessions and non-headless daemon sessions — those fall through to the
    # normal COMPLETED path unchanged.
    is_headless = session.origin is SessionOrigin.DAEMON and bool(
        context.get("headless")
    )
    now = datetime.now(UTC)
    parsed_sentinel: AutoDevResult | BlockedResult | None = None
    if is_headless:
        parsed_sentinel = _parse_sentinel_from_transcript(
            cwd_value, claude_session_id if isinstance(claude_session_id, str) else None
        )
        if parsed_sentinel is None:
            elapsed = (now - session.started_at).total_seconds()
            _headless_config = load_orchestrator_config()
            _stop_task: TicketTask | None = None
            if isinstance(ticket_id_value, str):
                _stop_store = load_dev_queue()
                _stop_task = next(
                    (t for t in _stop_store.tasks if t.ticket_id == ticket_id_value),
                    None,
                )
            _budget = resolve_headless_budget(_stop_task, session, _headless_config)
            if elapsed < _budget:
                # Under budget — defer. Another Stop hook turn will fire, or
                # reconcile will eventually catch a phantom and CRASH it.
                return
            # Budget exceeded without sentinel → TIMED_OUT (loud, retry-eligible).
            last_msg = hook_payload.get("last_assistant_message", "")
            excerpt = str(last_msg)[:500] if last_msg else ""
            session.status = SessionStatus.TIMED_OUT
            session.completed_at = now
            session.completed_reason = CompletionReason.TIMED_OUT
            if isinstance(claude_session_id, str):
                session.claude_session_id = claude_session_id
            save_state(state)
            timed_out_payload: dict[str, object] = {
                "session_id": session.id,
                "session_name": session.name,
                "client": context.get("client"),
                "ticket_id": ticket_id_value,
                "claude_session_id": claude_session_id,
                "elapsed_seconds": elapsed,
                "last_assistant_message_excerpt": excerpt,
            }
            record_event(OrchestratorEventType.SESSION_TIMED_OUT, timed_out_payload)
            # Revert the owning TicketTask from RUNNING → PENDING so the
            # dispatch loop can retry this ticket on the next tick.
            with dev_queue_lock():
                store = load_dev_queue()
                for task in store.tasks:
                    if (
                        task.ticket_id == ticket_id_value
                        and task.status == QueueItemStatus.RUNNING
                    ):
                        task.status = QueueItemStatus.PENDING
                        task.session_id = None
                        break
                save_dev_queue(store)
            if session.surface_ref is not None:
                get_native_daemon_client().stop(session.surface_ref)
            return

    # Issue #251: directly update the dev-queue task *before* marking the
    # session COMPLETED. This closes the race where revert_completed_silent_tasks
    # sees a COMPLETED session with a still-RUNNING task and reverts it to
    # PENDING before consume_completed_sessions can process the event — causing
    # no_op and similar terminal outcomes to trigger infinite re-dispatch.
    if is_headless and parsed_sentinel is not None and isinstance(ticket_id_value, str):
        _apply_sentinel_to_task(ticket_id_value, session.id, parsed_sentinel)

    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    if isinstance(claude_session_id, str):
        session.claude_session_id = claude_session_id
    # Issue #225: headless DAEMON sessions bypass the cw wrapper, so
    # signal_completed (wrapper.py) never runs and last_result stayed None.
    # Capture the parsed sentinel here before save_state so downstream
    # consumers (consume_completed_sessions, /cw-followup) can route by
    # status. parse_stdout returns BlockedResult on malformed payloads — we
    # persist either shape; both serialize to a dict with a "status" field.
    if parsed_sentinel is not None:
        session.last_result = parsed_sentinel.model_dump(mode="json")
    save_state(state)

    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": context.get("client"),
        "ticket_id": context.get("ticket_id"),
        "claude_session_id": claude_session_id,
        "hook_event": hook_payload.get("hook_event_name"),
        "crashed": False,
    }
    record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    # Native bg workers stay registered with the Claude daemon as
    # ``idle`` after their turn ends; without an explicit stop they
    # accumulate in roster.json across dispatches (the very failure
    # mode that motivated GitHub issue #150 in the first place). The
    # stop call is best-effort: native_daemon.stop logs and swallows
    # missing-binary / timeout errors rather than failing the hook.
    if session.origin is SessionOrigin.DAEMON and session.surface_ref is not None:
        get_native_daemon_client().stop(session.surface_ref)


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
    click.echo(
        "Note: cw daemon's PR-dispatch role is deprecated as of 0.11. "
        "The channel-based orchestrator (cw orchestrator-start, arriving in #115b) "
        "will replace this dispatch role. "
        "The PR watcher loop (this command's other role) is still required.",
        err=True,
    )
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
@click.option(
    "--timeout",
    "-t",
    "headless_timeout_override",
    type=int,
    default=None,
    help="Override headless timeout (seconds) for this ticket.",
)
@handle_errors
def dev_queue_add(
    tickets: tuple[str, ...],
    client: str | None,
    priority: int,
    headless_timeout_override: int | None,
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
        f"{'CLIENT':<20} {'PENDING':>7}  {'RUNNING':>7}  {'COMPLETED':>9}"
        f"  {'CANCELLED':>9}  TICKETS"
    )
    click.echo(header)
    click.echo("-" * 82)
    for client_name in clients_seen:
        client_tasks = by_client[client_name]
        pending_tasks = [t for t in client_tasks if t.status == QueueItemStatus.PENDING]
        running_tasks = [t for t in client_tasks if t.status == QueueItemStatus.RUNNING]
        completed_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.COMPLETED
        ]
        cancelled_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.CANCELLED
        ]
        ticket_ids = ", ".join(t.ticket_id for t in client_tasks)
        click.echo(
            f"{client_name:<20} {len(pending_tasks):>7}  {len(running_tasks):>7}"
            f"  {len(completed_tasks):>9}  {len(cancelled_tasks):>9}  {ticket_ids}"
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
@handle_errors
def dev_queue_run(
    max_parallel: int | None,
    once: bool,
    use_plan: bool,
    parent: str | None,
) -> None:
    """Run the dispatch loop, spawning sessions for pending tickets."""
    run_dispatch_loop(
        max_parallel=max_parallel,
        once=once,
        use_plan=use_plan,
        parent=parent,
    )


_PLAN_DEFAULT_TIMEOUT = 300


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
    exit_code = _run_plan_impl(
        client_name=client,
        timeout=timeout,
        client_filter=filter_client,
    )
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)


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
            before, after = fast_forward_main(client)
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

    lines.extend(("", f"Running sessions:  {len(status.running_sessions)}"))
    for s in status.running_sessions:
        line = f"  - {s.id}  {s.name}  status={s.status}"
        if s.last_stage:
            line += f"  last_stage={s.last_stage}"
        lines.append(line)

    lines.extend(("", f"Monitored PRs:     {len(status.monitored_prs)}"))
    lines.extend(
        f"  - {pr.repo}#{pr.pr_number}  status={pr.status}"
        f"  unresolved={pr.unresolved_threads}"
        for pr in status.monitored_prs
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
@click.option(
    "--compact",
    "level_compact",
    is_flag=True,
    default=False,
    help="One-line per-client summary (counts only).",
)
@click.option(
    "--verbose",
    "level_verbose",
    is_flag=True,
    default=False,
    help="Show extra columns: surface_ref, scope_hint, unresolved threads.",
)
@handle_errors
def orchestrate_watch(
    interval: int,
    client_filter: str | None,
    level_compact: bool,
    level_verbose: bool,
) -> None:
    """Render the orchestrator dashboard live, refreshing on an interval.

    Groups sessions, tickets, and PRs by client.  Press Ctrl-C to exit.
    """
    if level_compact and level_verbose:
        msg = "Pass at most one of --compact / --verbose."
        raise click.ClickException(msg)

    level = DetailLevel.DEFAULT
    if level_compact:
        level = DetailLevel.COMPACT
    elif level_verbose:
        level = DetailLevel.VERBOSE

    tui_watch(
        interval=interval,
        client_filter=client_filter,
        level=level,
        home=str(Path.home()),
    )


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


# --- Spawn command group ---


def _spawn_create_impl(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt_file: Path,
    label: str | None,
    headless: bool = False,
    native_daemon: NativeDaemonClient | None = None,
) -> str:
    """Create a daemon-spawned session.

    Separated from the Click command so tests can inject a fake daemon
    client directly. Reads the prompt from disk at this CLI boundary and
    inlines it into the spawn — keeping all file IO at the user-facing
    edge. Delegates the actual spawn to :func:`cw.spawn.spawn_create_impl`.

    Returns the new session's ID.
    """
    return spawn_create_impl(
        client=client,
        worktree=worktree,
        prompt=prompt_file.read_text(encoding="utf-8"),
        label=label,
        headless=headless,
        native_daemon=native_daemon,
    )


def _spawn_close_impl(
    *,
    session_id: str,
    native_daemon: NativeDaemonClient | None = None,
) -> None:
    """Close a daemon-spawned session.

    DAEMON-origin sessions are stopped via the native daemon client.
    USER-origin sessions with a legacy ``surface_ref`` are logged and
    skipped — the multiplexer adapter has been removed. Separated from
    the Click command so tests can inject the daemon client directly.
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
        if sess.origin is SessionOrigin.DAEMON:
            daemon = native_daemon or get_native_daemon_client()
            daemon.stop(sess.surface_ref)
        else:
            logging.getLogger(__name__).warning(
                "Session %s has legacy surface_ref %r; skipping surface close",
                sess.id,
                sess.surface_ref,
            )

    # For DAEMON sessions, atomically cancel any RUNNING TicketTask that owns
    # this session so revert_completed_silent_tasks cannot revert it to PENDING
    # and the dispatcher cannot re-spawn the same ticket in the same tick.
    # (See GitHub issue #317.)
    if sess.origin is SessionOrigin.DAEMON:
        cancel_task_for_session(sess.id)

    sess.status = SessionStatus.COMPLETED
    sess.completed_at = datetime.now(UTC)
    sess.completed_reason = CompletionReason.USER
    save_state(state)


@main.group(invoke_without_command=True)
@click.option("--client", "-c", default=None, help="Client name.")
@click.option("--worktree", "-w", default=None, help="Worktree path.")
@click.option("--prompt-file", "-f", default=None, help="Path to prompt file.")
@click.option("--label", "-l", default=None, help="Session label (default: daemon).")
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help=(
        "Mark the session as headless in cw-context.json so the signal_stop "
        "Layer 1 backstop (issue #176) activates: a session that exits without "
        "an AUTO_DEV_RESULT sentinel within the 30-min budget transitions to "
        "TIMED_OUT (retry-eligible) instead of silently COMPLETED. Use when the "
        "prompt invokes /auto-dev --headless or any other skill that emits the "
        "sentinel contract."
    ),
)
@click.pass_context
@handle_errors
def spawn(
    ctx: click.Context,
    client: str | None,
    worktree: str | None,
    prompt_file: str | None,
    label: str | None,
    headless: bool,
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
    session_id = _spawn_create_impl(
        client=client_config,
        worktree=Path(cast("str", worktree)),
        prompt_file=Path(cast("str", prompt_file)),
        label=label,
        headless=headless,
    )
    click.echo(session_id)


@spawn.command(name="close")
@click.argument("session_id")
@handle_errors
def spawn_close(session_id: str) -> None:
    """Close a spawned session by session ID.

    Stops the session via the native daemon and marks it as COMPLETED.

    \b
    Example:
      cw spawn close abc12345
    """
    _spawn_close_impl(session_id=session_id)
    click.echo(f"Closed session: {session_id}")


def _spawn_complete_impl(
    *,
    session_id: str,
    status: str,
    ticket_id: str | None,
    force: bool,
    native_daemon: NativeDaemonClient | None = None,
) -> None:
    """Complete a daemon-spawned session atomically.

    Performs three steps in sequence:
    1. Records a session.completed event
    2. Applies it to the dev queue inline (under dev_queue_lock)
    3. Closes the session

    Separated from the Click command so tests can inject the daemon client
    directly.
    """
    state = load_state()
    sess = state.find_by_name_or_id(session_id)
    if sess is None:
        msg = f"Session '{session_id}' not found."
        raise CwError(msg)

    if sess.status == SessionStatus.COMPLETED:
        if not force:
            msg = f"Session '{session_id}' is already completed. Use --force to no-op."
            raise CwError(msg)
        return

    effective_ticket_id = ticket_id or ticket_id_for_session(sess.name)

    payload: dict[str, object] = {
        "session_id": sess.id,
        "client": sess.client,
        "crashed": False,
        "status": status,
        **({"ticket_id": effective_ticket_id} if effective_ticket_id else {}),
    }

    with dev_queue_lock():
        store = load_dev_queue()

        # Guard: queue task already COMPLETED (inside lock — authoritative)
        if effective_ticket_id:
            for task in store.tasks:
                if (
                    task.ticket_id == effective_ticket_id
                    and task.status == QueueItemStatus.COMPLETED
                ):
                    already_done_task_msg = (
                        f"Queue task for '{effective_ticket_id}' is already COMPLETED. "
                        "Use 'cw spawn close' to close the session only."
                    )
                    raise CwError(already_done_task_msg)

        # Step 1: Record event (record_event uses _inbox_lock — no deadlock risk)
        event = record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

        # Step 2: Apply to queue
        _apply_events_to_store(store, [event])

        # Step 3: Close session state
        sess.status = SessionStatus.COMPLETED
        sess.completed_at = datetime.now(UTC)
        if sess.completed_reason is None:
            sess.completed_reason = CompletionReason.USER
        save_state(state)

    # Advance cursor after lock (advance_cursor uses inbox lock — safe)
    advance_cursor(_DISPATCH_CONSUMER, event.id)

    # daemon.stop outside lock — best-effort, slow (up to 10s timeout)
    daemon = native_daemon or get_native_daemon_client()
    if sess.surface_ref is not None and sess.origin is SessionOrigin.DAEMON:
        daemon.stop(sess.surface_ref)


@spawn.command(name="complete")
@click.argument("session_id")
@click.option(
    "--status",
    "status",
    required=True,
    type=click.Choice(list(get_args(Status))),
    help="Outcome status to record (every value in auto_dev_result.Status).",
)
@click.option(
    "--ticket-id",
    default=None,
    help=(
        "Ticket ID; inferred from session name via ticket_id_for_session() if omitted."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "If session is already COMPLETED, silently succeed (no-op) instead of erroring."
    ),
)
@handle_errors
def spawn_complete(
    session_id: str,
    status: str,
    ticket_id: str | None,
    force: bool,
) -> None:
    """Complete a daemon-spawned session, recording its outcome atomically.

    Records a session.completed event, applies it to the dev queue, and
    closes the session in one atomic operation. Replaces the three-command
    manual recovery sequence used when sessions are stuck.

    \b
    Example:
      cw spawn complete abc12345 --status shipped
      cw spawn complete abc12345 --status blocked --ticket-id GEN-42
    """
    _spawn_complete_impl(
        session_id=session_id,
        status=status,
        ticket_id=ticket_id,
        force=force,
    )
    click.echo(f"Completed session: {session_id} (status={status})")


_ORCHESTRATOR_AGENT = "cw-orchestrator"
_ORCHESTRATOR_CHANNEL = "server:cw-pr-events"


def _resolve_client(client_name: str | None) -> ClientConfig:
    """Resolve --client to a ClientConfig, defaulting to the first configured client."""
    clients = load_clients()
    if client_name:
        return get_client(client_name)
    if not clients:
        msg = "No clients configured. Add one to ~/.config/cw/clients.yaml."
        raise CwError(msg)
    return next(iter(clients.values()))


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


_PEEK_DEFAULT_LINES = 50
_PEEK_DEFAULT_SCROLLBACK = 200


def _peek_session(
    session_name: str,
    *,
    lines: int,
    scrollback: int,
    adapter: MultiplexerAdapter | None = None,
) -> None:
    """Emit the last *lines* lines of worker output for *session_name*.

    Raises :exc:`cw.exceptions.CwError` when the session is not found,
    is already completed, or its surface is no longer live.
    """
    state = load_state()
    session = state.find_by_name_or_id(session_name)
    if session is None:
        msg = f"Session '{session_name}' not found."
        raise CwError(msg)
    if session.status == SessionStatus.COMPLETED:
        msg = (
            f"Session '{session_name}' is completed (status: completed)."
            " Its surface is no longer available."
        )
        raise CwError(msg)
    if session.surface_ref is None:
        msg = f"Session '{session_name}' has no surface reference recorded."
        raise CwError(msg)
    _adapter = adapter or get_cmux_adapter()
    live = _adapter.list_surfaces()
    if session.surface_ref not in live:
        msg = (
            f"Surface '{session.surface_ref}' for session '{session_name}' is gone."
            f" Run 'cw post-mortem {session.id}' for the full transcript"
            " once that command is available."
        )
        raise CwError(msg)
    content = _adapter.capture_surface(
        session.surface_ref, lines=lines, scrollback=scrollback
    )
    stripped = content.strip()
    actual_lines = len(content.splitlines()) if stripped else 0
    if actual_lines < lines and stripped:
        click.echo(
            f"Warning: fewer than {lines} lines available in scrollback"
            f" (got {actual_lines}).",
            err=True,
        )
    click.echo(content, nl=False)


@main.command()
@click.argument("session_name", shell_complete=_complete_session)
@click.option(
    "--lines",
    "-n",
    default=_PEEK_DEFAULT_LINES,
    show_default=True,
    help="Number of lines to emit from the end of worker output.",
)
@click.option(
    "--scrollback",
    "-s",
    default=_PEEK_DEFAULT_SCROLLBACK,
    show_default=True,
    help="Maximum scrollback depth to search (lines).",
)
@handle_errors
def peek(session_name: str, lines: int, scrollback: int) -> None:
    """Emit the last N lines of worker output for a session."""
    _peek_session(session_name, lines=lines, scrollback=scrollback)


@main.group(name="pr-channel")
def pr_channel() -> None:
    """PR channel server: push MCP notifications to subscribed Claude sessions."""


@pr_channel.command(name="proxy")
@click.option("--client-id", default=None, help="Unique client ID for cursor tracking.")
def pr_channel_proxy(client_id: str | None) -> None:
    """Start the MCP stdio proxy for cw-pr-events (add to .mcp.json)."""
    from cw.cw_pr_events_channel import run_proxy  # noqa: PLC0415

    run_proxy(client_id=client_id)


@pr_channel.command(name="serve")
@click.option("--port", default=8788, type=int, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
def pr_channel_serve(port: int, host: str) -> None:
    """Start the cw-pr-events MCP channel server.

    Defaults mirror ``cw.cw_pr_events_server.DEFAULT_HOST`` / ``DEFAULT_PORT`` —
    kept inline here so the click decorators don't trigger an eager import of
    ``starlette`` (lives in the ``[mcp]`` optional-deps extra).
    """
    from cw.cw_pr_events_server import serve as _serve  # noqa: PLC0415

    _serve(host=host, port=port)


@main.group(name="queue-channel")
def queue_channel() -> None:
    """Queue channel server: push MCP notifications to subscribed Claude sessions."""


@queue_channel.command(name="proxy")
@click.option("--client-id", default=None, help="Unique client ID for cursor tracking.")
def queue_channel_proxy(client_id: str | None) -> None:
    """Start the MCP stdio proxy for cw-queue-events (add to .mcp.json)."""
    from cw.cw_queue_events_channel import run_proxy  # noqa: PLC0415

    run_proxy(client_id=client_id)


@queue_channel.command(name="serve")
@click.option("--port", default=8789, type=int, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
def queue_channel_serve(port: int, host: str) -> None:
    """Start the cw-queue-events MCP channel server.

    Defaults mirror ``cw.cw_queue_events_server.DEFAULT_HOST`` / ``DEFAULT_PORT`` —
    kept inline here so the click decorators don't trigger an eager import of
    ``starlette`` (lives in the ``[mcp]`` optional-deps extra).
    """
    from cw.cw_queue_events_server import serve as _serve  # noqa: PLC0415

    _serve(host=host, port=port)
