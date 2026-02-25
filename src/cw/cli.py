"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
from click.shell_completion import CompletionItem

from cw import __version__, zellij
from cw.config import (
    get_client,
    init_client,
    load_clients,
    load_state,
    save_state,
    show_config,
)
from cw.daemon import daemon_status as _daemon_status_fn
from cw.daemon import start_daemon, start_daemon_all, stop_daemon
from cw.exceptions import CwError
from cw.history import EventType, load_history
from cw.hooks import (
    add_event_hook,
    install_context_hook,
    list_event_hooks,
    remove_event_hook,
    uninstall_context_hook,
)
from cw.hooks import hook_status as _hook_status_fn
from cw.models import (
    CompletionReason,
    CwState,
    QueueItem,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)
from cw.plan import find_plan_files, parse_plan
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
    CW_SESSION,
    background_all_sessions,
    background_session,
    delegate_task,
    done_session,
    hand_to_session,
    handoff_session,
    resume_session,
    start_session,
)
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
        CompletionItem(name)
        for name in load_clients()
        if name.startswith(incomplete)
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
    "--worktree", "-w",
    default=None,
    help="Git branch for worktree isolation (e.g. feat/search).",
)
@handle_errors
def start(client: str, purpose: str, worktree: str | None) -> None:
    """Start or resume a Claude Code session for a client."""
    start_session(client, purpose, worktree=worktree)


@main.command()
@click.argument("session_name", required=False, default=None,
                shell_complete=_complete_session)
@click.option(
    "--notify", "-n",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Notify a sibling session after backgrounding.",
)
@click.option(
    "--auto", is_flag=True, default=False,
    help="Mark as auto-backgrounded (used by hooks).",
)
@click.option(
    "--all", "all_sessions", is_flag=True, default=False,
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
    "target",
    type=click.Choice([e.value for e in SessionPurpose]),
)
@click.argument("message")
@click.option(
    "--from",
    "source",
    type=click.Choice([e.value for e in SessionPurpose]),
    default=None,
    help="Source session (for audit trail).",
)
@handle_errors
def hand(target: str, message: str, source: str | None) -> None:
    """Hand off a message to another session.

    Example: cw hand debt "Fix the ruff violations in session.py"
    """
    hand_to_session(target, message, source_purpose=source)


@main.command()
@click.argument("session_name", required=False, default=None,
                shell_complete=_complete_session)
@click.option("--cleanup", is_flag=True, help="Remove associated worktree.")
@click.option("--force", is_flag=True, help="Force worktree removal.")
@handle_errors
def done(session_name: str | None, cleanup: bool, force: bool) -> None:
    """Mark a session as completed (not resumable).

    Optionally removes the associated worktree with --cleanup.
    """
    done_session(session_name, cleanup=cleanup, force=force)


def _parse_handoff_route(
    source: str,
    target: str | None,
) -> tuple[str, str]:
    """Parse handoff route from positional args or arrow syntax."""
    if "->" in source:
        parts = source.split("->", 1)
        return parts[0].strip(), parts[1].strip()
    if target:
        return source, target
    msg = "Handoff requires source and target: cw handoff impl idea"
    raise CwError(msg)


@main.command()
@click.argument("source", required=True)
@click.argument("target", required=False, default=None)
@click.option(
    "--client", "-c",
    default=None,
    shell_complete=_complete_client,
    help="Explicit client name (auto-detected if omitted).",
)
@handle_errors
def handoff(source: str, target: str | None, client: str | None) -> None:
    """Hand off context from one session to another.

    Backgrounds the source and delivers context to the target.

    \b
    Examples:
      cw handoff impl idea
      cw handoff impl->idea
      cw handoff impl idea --client sigma
    """
    src, tgt = _parse_handoff_route(source, target)
    handoff_session(src, tgt, client_name=client)


@main.command()
@handle_errors
def config() -> None:
    """Show current configuration."""
    show_config()


@main.command(name="init")
@click.argument("name", required=False, default=None)
@click.option(
    "--path", "-p",
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
            "Path is required: use --path or run"
            " without arguments for interactive mode"
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


@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.option("--all", "show_all", is_flag=True, help="Include completed plans.")
@handle_errors
def plan(client: str, show_all: bool) -> None:
    """Show plan progress for a client workspace.

    Parses .claude/plans/ markdown files for checkbox progress.
    """
    client_config = get_client(client)
    plans = find_plan_files(client_config.workspace_path)
    if not plans:
        click.echo(f"No plans found for {client}.")
        return

    for plan_path in plans:
        summary = parse_plan(plan_path)
        done, total = summary.progress
        if total == 0:
            if show_all:
                click.echo(f"{summary.title} (no tasks)")
            continue
        pct = int(done / total * 100)
        if not show_all and pct == 100:
            continue
        click.echo(f"{summary.title} [{done}/{total}] {pct}%")
        for phase in summary.phases:
            p_done, p_total = phase.progress
            if p_total == 0:
                continue
            label = "Done" if p_done == p_total else f"{p_done}/{p_total}"
            click.echo(f"  {phase.name}: {label}")


def _parse_since(since: str | None) -> datetime | None:
    """Parse a relative time string like '2h', '1d', '7d' into a datetime."""
    if since is None:
        return None

    now = datetime.now(UTC)
    value = since[:-1]
    unit = since[-1]
    try:
        n = int(value)
    except ValueError:
        msg = f"Invalid --since value: {since} (expected e.g. 2h, 1d, 7d)"
        raise CwError(msg) from None
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(days=n)
    msg = f"Invalid --since unit: {unit} (expected h or d)"
    raise CwError(msg)


@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.option("--limit", "-n", type=int, default=20, help="Max events to show.")
@click.option("--since", default=None, help="Filter by time (e.g. 2h, 1d, 7d).")
@click.option("--type", "event_type", default=None, help="Comma-separated event types.")
@handle_errors
def history(client: str, limit: int, since: str | None, event_type: str | None) -> None:
    """Show event history for a client.

    \b
    Examples:
      cw history my-client
      cw history my-client --limit 50
      cw history my-client --since 2h
      cw history my-client --type session_started,session_completed
    """
    since_dt = _parse_since(since)
    type_filter = None
    if event_type:
        type_filter = [EventType(t.strip()) for t in event_type.split(",")]

    events = load_history(client, since=since_dt, event_types=type_filter, limit=limit)
    if not events:
        click.echo(f"No history events for {client}.")
        return

    for event in events:
        ts = _relative_time(event.timestamp)
        detail = f" - {event.detail}" if event.detail else ""
        session = f" [{event.session_name}]" if event.session_name else ""
        click.echo(f"{ts:<12} {event.event_type:<24}{session}{detail}")


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


def _check_and_mark_dead_sessions(state: CwState) -> list[Session]:
    """Check active sessions for dead panes, mark them COMPLETED.

    Inspects each client's tab individually so multi-tab sessions
    are checked correctly.
    """
    if not zellij.session_exists(CW_SESSION):
        return []

    dead: list[Session] = []
    now = datetime.now(UTC)
    # Cache health per client tab to avoid repeated dump-layout calls
    tab_health: dict[str, dict[str, bool]] = {}
    for s in state.active_sessions():
        tab = s.zellij_tab or s.client
        if tab not in tab_health:
            tab_health[tab] = zellij.check_pane_health(
                session=CW_SESSION, tab_name=tab,
            )
        health = tab_health[tab]
        pane_name = s.zellij_pane or s.purpose
        if pane_name in health and not health[pane_name]:
            s.status = SessionStatus.COMPLETED
            s.completed_reason = CompletionReason.CRASHED
            s.completed_at = now
            dead.append(s)

    if dead:
        save_state(state)

    return dead


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
                f" handoff: {s.last_handoff_path.name}"
                if s.last_handoff_path
                else ""
            )
            click.echo(f"  {s.name}{handoff}")


# --- Queue command group ---

@main.group()
def queue() -> None:
    """Manage the task queue for delegation and daemon processing."""


@queue.command(name="add")
@click.argument("client", shell_complete=_complete_client)
@click.argument("description")
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Queue purpose.",
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
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default=None, help="Filter by purpose.",
)
@click.option(
    "--status", "status_filter",
    type=click.Choice([e.value for e in QueueItemStatus]),
    default=None, help="Filter by status.",
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
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default=None, help="Clear only items with this purpose.",
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
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default=None, help="Filter by purpose.",
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
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default=None, help="Filter by purpose.",
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


# --- Delegate command ---

@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.argument("description")
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Task purpose.",
)
@click.option("--prompt", default=None, help="Exact prompt for Claude.")
@click.option("--interactive", is_flag=True, help="Leave Claude in interactive mode.")
@click.option(
    "--context-files", default=None,
    help="Comma-separated file paths for context.",
)
@click.option("--priority", type=int, default=0, help="Priority (higher = sooner).")
@handle_errors
def delegate(
    client: str,
    description: str,
    purpose: str,
    prompt: str | None,
    interactive: bool,
    context_files: str | None,
    priority: int,
) -> None:
    """Delegate a task to a new Zellij pane running Claude.

    Routes to an existing backgrounded session if one is available,
    otherwise spawns a new pane.

    \b
    Examples:
      cw delegate my-client "Fix ruff violations" --purpose debt
      cw delegate my-client "Run pytest" --purpose impl --interactive
      cw delegate my-client "Urgent fix" --priority 10
    """
    files = context_files.split(",") if context_files else None
    delegate_task(
        client, description,
        purpose=purpose, prompt=prompt,
        context_files=files, interactive=interactive,
        priority=priority,
    )


# --- Hook command group ---

@main.group()
def hook() -> None:
    """Manage auto-background hooks for context monitoring."""


@hook.command(name="install")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--threshold", type=int, default=None,
    help="Turn count threshold (overrides client config).",
)
@handle_errors
def hook_install(client: str, threshold: int | None) -> None:
    """Install a context monitoring hook for a client."""
    if threshold is None:
        client_config = get_client(client)
        threshold = client_config.auto_background_threshold
    if threshold is None:
        threshold = 50  # Default if not configured
    install_context_hook(client, threshold)


@hook.command(name="uninstall")
@click.argument("client", shell_complete=_complete_client)
@handle_errors
def hook_uninstall(client: str) -> None:
    """Remove the context monitoring hook for a client."""
    uninstall_context_hook(client)


@hook.command(name="status")
@click.argument("client", shell_complete=_complete_client)
@handle_errors
def hook_status(client: str) -> None:
    """Show hook configuration for a client."""
    info = _hook_status_fn(client)
    installed = "Yes" if info["installed"] else "No"
    click.echo(f"Installed: {installed}")
    click.echo(f"Script:    {info['script_path']}")
    click.echo(f"Turns:     {info['turn_count']}")


@hook.command(name="add")
@click.argument("client", shell_complete=_complete_client)
@click.argument(
    "event_type",
    type=click.Choice([e.value for e in EventType]),
)
@click.argument("command")
@click.option("--description", "-d", default="", help="Human-readable description.")
@handle_errors
def hook_add(
    client: str,
    event_type: str,
    command: str,
    description: str,
) -> None:
    """Add an event hook that runs a shell command on lifecycle events.

    \b
    Examples:
      cw hook add personal session_backgrounded "echo bg >> /tmp/cw.log"
      cw hook add personal queue_item_completed "notify-send 'Done!'"
    """
    rule = add_event_hook(client, event_type, command, description=description)
    click.echo(f"Added hook: {rule.event_type} -> {rule.command}")


@hook.command(name="remove")
@click.argument("client", shell_complete=_complete_client)
@click.argument(
    "event_type",
    type=click.Choice([e.value for e in EventType]),
)
@handle_errors
def hook_remove(client: str, event_type: str) -> None:
    """Remove all event hooks for a given event type."""
    removed = remove_event_hook(client, event_type)
    if removed == 0:
        click.echo(f"No hooks found for event type '{event_type}'.")
    else:
        click.echo(f"Removed {removed} hook(s) for '{event_type}'.")


@hook.command(name="list")
@click.argument("client", shell_complete=_complete_client)
@handle_errors
def hook_list(client: str) -> None:
    """List all event hooks for a client."""
    rules = list_event_hooks(client)
    if not rules:
        click.echo("No event hooks configured.")
        return

    click.echo(f"{'EVENT TYPE':<28} {'COMMAND'}")
    click.echo("-" * 60)
    for rule in rules:
        click.echo(f"{rule.event_type:<28} {rule.command}")


# --- Plugin command group ---

@main.group()
def plugin() -> None:
    """Build and install the Zellij status bar plugin."""


@plugin.command(name="build")
@click.option(
    "--plugin-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None, help="Path to zellij-plugin source directory.",
)
@handle_errors
def plugin_build(plugin_dir: Path | None) -> None:
    """Build the WASM status bar plugin from Rust source.

    Requires cargo (Rust toolchain) to be installed.
    Auto-detects the plugin source directory in editable installs;
    pass --plugin-dir for non-editable installs.
    """
    from cw.plugin import build_plugin

    click.echo("Building Zellij plugin...")
    wasm_path = build_plugin(plugin_dir=plugin_dir)
    click.echo(f"Built: {wasm_path}")


@plugin.command(name="install")
@click.option(
    "--build/--no-build", "do_build", default=True,
    help="Build before installing (default: yes).",
)
@click.option(
    "--plugin-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None, help="Path to zellij-plugin source directory.",
)
@handle_errors
def plugin_install(do_build: bool, plugin_dir: Path | None) -> None:
    """Build and install the WASM plugin to Zellij's plugin directory.

    \b
    After install, restart Zellij to pick up the plugin:
      cw start <client>
    """
    from cw.plugin import build_plugin, install_plugin

    if do_build:
        click.echo("Building Zellij plugin...")
        wasm_path = build_plugin(plugin_dir=plugin_dir)
        click.echo(f"Built: {wasm_path}")
    else:
        wasm_path = None

    dest = install_plugin(wasm_path=wasm_path, plugin_dir=plugin_dir)
    click.echo(f"Installed: {dest}")


# --- Daemon command group ---

@main.group()
def daemon() -> None:
    """Manage the background task processing daemon."""


@daemon.command(name="start")
@click.argument("client", required=False, default=None,
                shell_complete=_complete_client)
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Purpose to process.",
)
@click.option("--poll-interval", type=int, default=30, help="Seconds between polls.")
@click.option(
    "--no-bootstrap", is_flag=True, default=False,
    help="Don't auto-start sessions when none exist.",
)
@handle_errors
def daemon_start(
    client: str | None,
    purpose: str,
    poll_interval: int,
    no_bootstrap: bool,
) -> None:
    """Start the daemon to process queued tasks.

    With no arguments, monitors all client queues.
    With CLIENT, monitors a specific client/purpose.

    Runs in the foreground — designed to be spawned in a Zellij pane.
    """
    if client is None:
        start_daemon_all(poll_interval=poll_interval)
    else:
        start_daemon(
            client, purpose,
            poll_interval=poll_interval,
            auto_bootstrap=not no_bootstrap,
        )


@daemon.command(name="stop")
@click.argument("client", required=False, default=None,
                shell_complete=_complete_client)
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Purpose daemon to stop.",
)
@handle_errors
def daemon_stop(client: str | None, purpose: str) -> None:
    """Stop a running daemon.

    With no arguments, stops the all-queue daemon.
    With CLIENT, stops the daemon for that client/purpose.
    """
    if client is None:
        stop_daemon("_all", "_all")
    else:
        stop_daemon(client, purpose)


@daemon.command(name="status")
@click.argument("client", required=False, default=None, shell_complete=_complete_client)
@handle_errors
def daemon_status_cmd(client: str | None) -> None:
    """Show running daemons."""
    daemons = _daemon_status_fn(client)
    if not daemons:
        click.echo("No daemons running.")
        return

    click.echo(f"{'CLIENT':<18} {'PURPOSE':<10} {'PID':<8} {'STATUS'}")
    click.echo("-" * 50)
    for d in daemons:
        status_label = "alive" if d["alive"] else "dead"
        pid_str = str(d["pid"])
        click.echo(
            f"{d['client']:<18} {d['purpose']:<10}"
            f" {pid_str:<8} {status_label}"
        )


@main.command()
@handle_errors
def dashboard() -> None:
    """Launch the interactive TUI dashboard.

    Shows sessions, activity feed, and status across all clients.
    Keys: r=Resume, b=Background, d=Done, q=Quit.
    """
    # Lazy: avoid loading Textual for non-TUI commands
    from cw.tui import run_dashboard

    run_dashboard()


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
