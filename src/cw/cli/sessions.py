"""Session lifecycle commands.

Covers ``start``/``bg``/``resume``/``list``/``status``/``watch``/``done``/
``guide``/``peek`` plus the ``daemon`` deprecation shim and ``completion``.
The ``signal-stop`` Stop-hook backstop lives in :mod:`cw.cli.stop_hook`.
"""

from __future__ import annotations

import importlib.resources
from collections import deque
from pathlib import Path

import click

from cw._util import _iter_assistant_text_blocks, claude_project_dir
from cw.cli._base import (
    _complete_client,
    _complete_session,
    _emit_freshness_subline,
    _relative_time,
    handle_errors,
    main,
)
from cw.config import load_clients, load_state
from cw.exceptions import CwError
from cw.models import (
    WORKER_PURPOSES,
    DispatchSkipReason,
    SessionStatus,
)
from cw.orchestrate import latest_tick_summary_by_client
from cw.reconcile import reconcile
from cw.session import (
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)
from cw.tui import watch_flat


@main.command()
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
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
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
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


@main.command(name="guide")
def guide() -> None:
    """Print the cw operator guide (how to drive a sprint with cw)."""
    text = (
        importlib.resources.files("cw")
        .joinpath("data/GUIDE.md")
        .read_text(encoding="utf-8")
    )
    click.echo(text)


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

    tick_data = latest_tick_summary_by_client()
    gated = {
        client_name: tick
        for client_name, tick in tick_data.items()
        if tick.skip_reason == DispatchSkipReason.FRESHNESS_GATE
    }
    if gated:
        click.echo("Freshness gates (action required):")
        for client_name, tick in gated.items():
            _emit_freshness_subline(
                client_name,
                tick.freshness_detail,
                tick.blocked_branch,
                tick.pending,
            )

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
            click.echo(f"  {s.name}")


@main.command(name="daemon")
@click.option(
    "--once",
    is_flag=True,
    default=False,
    expose_value=False,
    help="Accepted for backwards compatibility; has no effect.",
)
@handle_errors
def daemon() -> None:
    """Deprecated no-op — emits a notice and exits.

    ``cw daemon`` never dispatched dev-queue tickets. To dispatch the
    dev-queue, use ``cw dev-queue run``.
    """
    click.echo(
        "Note: `cw daemon` is deprecated and has no effect — it never dispatched\n"
        "dev-queue tickets. To dispatch the dev-queue, use `cw dev-queue run`.",
        err=True,
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


_PEEK_DEFAULT_LINES = 50
_PEEK_DEFAULT_SCROLLBACK = 200


def _peek_session(
    session_name: str,
    *,
    lines: int,
    scrollback: int,
) -> None:
    """Emit the last *lines* lines of worker output for *session_name*.

    Worker output is read from the session's Claude transcript
    (``~/.claude/projects/<encoded-cwd>/<claude_session_id>.jsonl``) rather
    than a multiplexer pane — dispatched workers run under the native daemon
    with no pane to scrape (see #504). Only assistant text blocks are
    surfaced; *scrollback* bounds how many trailing transcript output lines
    are considered before tailing the last *lines* of them. ``scrollback=0``
    means "no limit" — keep every output line.

    Raises :exc:`cw.exceptions.CwError` when the session is not found,
    is already completed, has no recorded Claude session id, or has no
    readable transcript.
    """
    state = load_state()
    session = state.find_by_name_or_id(session_name)
    if session is None:
        msg = f"Session '{session_name}' not found."
        raise CwError(msg)
    if session.status == SessionStatus.COMPLETED:
        msg = (
            f"Session '{session_name}' is completed (status: completed)."
            f" Run 'cw post-mortem {session.id}' for its transcript"
            " once that command is available."
        )
        raise CwError(msg)
    if session.claude_session_id is None:
        msg = f"Session '{session_name}' has no Claude session id recorded."
        raise CwError(msg)
    cwd = session.worktree_path or session.workspace_path
    transcript_path = claude_project_dir(cwd) / f"{session.claude_session_id}.jsonl"
    if not transcript_path.is_file():
        msg = (
            f"Transcript for session '{session_name}' not found at"
            f" {transcript_path}. Run 'cw post-mortem {session.id}' for the full"
            " transcript once that command is available."
        )
        raise CwError(msg)
    # Bound peak memory to the scrollback window: a long-running session's
    # transcript can be many MB, but peek only ever shows the tail. The
    # deque drops older lines as it fills (maxlen=None keeps everything).
    window: deque[str] = deque(maxlen=scrollback or None)
    for text in _iter_assistant_text_blocks(transcript_path):
        window.extend(text.splitlines())
    content = "\n".join(list(window)[-lines:])
    if (
        len(window) < lines
        and content.strip()
        and not (scrollback and scrollback < lines)
    ):
        click.echo(
            f"Warning: fewer than {lines} lines available in transcript"
            f" (got {len(window)}).",
            err=True,
        )
    # Why: content is newline-joined with no trailing newline; click.echo
    # adds exactly one, matching terminal output convention (the old pane
    # path used nl=False because the adapter returned raw, newline-framed
    # scrollback).
    click.echo(content)


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
    help="Max lines of transcript to scan. 0 = no limit (whole transcript).",
)
@handle_errors
def peek(session_name: str, lines: int, scrollback: int) -> None:
    """Emit the last N lines of worker output for a session."""
    _peek_session(session_name, lines=lines, scrollback=scrollback)
