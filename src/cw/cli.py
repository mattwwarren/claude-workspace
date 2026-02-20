"""Click CLI dispatcher for cw commands."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, datetime

import click
from click.shell_completion import CompletionItem

from cw import __version__, zellij
from cw.config import get_client, load_clients, load_state, save_state, show_config
from cw.daemon import daemon_status as _daemon_status_fn
from cw.daemon import start_daemon, stop_daemon
from cw.exceptions import CwError
from cw.hooks import hook_status as _hook_status_fn
from cw.hooks import install_context_hook, uninstall_context_hook
from cw.models import (
    CompletionReason,
    CwState,
    QueueItemStatus,
    Session,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)
from cw.plan import find_plan_files, parse_plan
from cw.queue import add_item, clear_queue, load_queue, remove_item
from cw.session import (
    CW_SESSION,
    background_session,
    delegate_task,
    done_session,
    hand_to_session,
    handoff_session,
    resume_session,
    start_session,
)
from cw.zellij import go_to_tab


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
@handle_errors
def bg(notify: str | None, auto: bool) -> None:
    """Background the current session (auto-handoff)."""
    background_session(notify=notify, auto=auto)


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
@click.argument("client", shell_complete=_complete_client)
@handle_errors
def switch(client: str) -> None:
    """Switch to a client's Zellij tab."""
    go_to_tab(client)


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
    msg = "Handoff requires source and target: cw handoff impl review"
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
      cw handoff impl review
      cw handoff impl->review
      cw handoff impl review --client sigma
    """
    src, tgt = _parse_handoff_route(source, target)
    handoff_session(src, tgt, client_name=client)


@main.command()
@handle_errors
def config() -> None:
    """Show current configuration."""
    show_config()


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
        elif s.status == SessionStatus.BACKGROUNDED:
            since = _relative_time(s.backgrounded_at or s.started_at)
        else:
            since = _relative_time(s.started_at)

        click.echo(f"{s.client:<18} {s.purpose:<10} {s.status:<14} {s.id:<10} {since}")


def _check_and_mark_dead_sessions(state: CwState) -> list[Session]:
    """Check active sessions for dead panes, mark them COMPLETED."""
    if not zellij.session_exists(CW_SESSION):
        return []

    health = zellij.check_pane_health(session=CW_SESSION)
    if not health:
        return []

    dead: list[Session] = []
    now = datetime.now(UTC)
    for s in state.active_sessions():
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
    backgrounded = state.backgrounded_sessions()

    click.echo(f"Clients configured: {len(clients)}")
    click.echo(f"Active sessions:    {len(active)}")
    click.echo(f"Backgrounded:       {len(backgrounded)}")
    click.echo()

    if active:
        click.echo("Active:")
        for s in active:
            since = _relative_time(s.resumed_at or s.started_at)
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
@handle_errors
def queue_add(client: str, description: str, purpose: str, prompt: str | None) -> None:
    """Add a work item to the queue."""
    task = TaskSpec(
        description=description,
        purpose=SessionPurpose(purpose),
        prompt=prompt or description,
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
@handle_errors
def delegate(
    client: str,
    description: str,
    purpose: str,
    prompt: str | None,
    interactive: bool,
    context_files: str | None,
) -> None:
    """Delegate a task to a new Zellij pane running Claude.

    \b
    Examples:
      cw delegate my-client "Fix ruff violations" --purpose debt
      cw delegate my-client "Run pytest" --purpose impl --interactive
    """
    files = context_files.split(",") if context_files else None
    delegate_task(
        client, description,
        purpose=purpose, prompt=prompt,
        context_files=files, interactive=interactive,
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


# --- Daemon command group ---

@main.group()
def daemon() -> None:
    """Manage the background task processing daemon."""


@daemon.command(name="start")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Purpose to process.",
)
@click.option("--poll-interval", type=int, default=30, help="Seconds between polls.")
@click.option("--review", is_flag=True, help="Pause after each item for review.")
@handle_errors
def daemon_start(
    client: str,
    purpose: str,
    poll_interval: int,
    review: bool,
) -> None:
    """Start the daemon to process queued tasks.

    Runs in the foreground — designed to be spawned in a Zellij pane.
    """
    start_daemon(client, purpose, poll_interval=poll_interval, review=review)


@daemon.command(name="stop")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose", type=click.Choice([e.value for e in SessionPurpose]),
    default="debt", help="Purpose daemon to stop.",
)
@handle_errors
def daemon_stop(client: str, purpose: str) -> None:
    """Stop a running daemon."""
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
