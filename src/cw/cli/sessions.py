"""Session lifecycle commands and the Stop-hook backstop.

Covers ``start``/``bg``/``resume``/``list``/``status``/``watch``/``done``/
``guide``/``peek`` plus the ``signal-stop`` Stop-hook handler, the ``daemon``
deprecation shim, and ``completion``.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw._util import _iter_assistant_text_blocks, claude_project_dir
from cw.cli._base import (
    _complete_client,
    _complete_session,
    _relative_time,
    handle_errors,
    main,
)
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.config import (
    load_clients,
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    WORKER_PURPOSES,
    CompletionReason,
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    _apply_sentinel_to_task,
    reconcile,
    resolve_headless_budget,
)
from cw.session import (
    background_all_sessions,
    background_session,
    done_session,
    resume_session,
    start_session,
)
from cw.tui import watch_flat

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult, BlockedResult

logger = logging.getLogger(__name__)


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


def _read_stop_hook_payload() -> tuple[dict[str, object], str] | None:
    """Read the Stop-hook JSON from stdin and extract its ``cwd``.

    Returns ``(hook_payload, cwd_value)`` when stdin holds a JSON object with a
    string ``cwd``, else ``None``. Best-effort: every failure mode (unreadable
    stdin, empty body, malformed JSON, missing cwd) is a silent no-op.
    """
    try:
        stdin_text = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if not stdin_text:
        return None
    try:
        hook_payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return None
    cwd_value = hook_payload.get("cwd") if isinstance(hook_payload, dict) else None
    if not isinstance(cwd_value, str):
        return None
    return hook_payload, cwd_value


def _resolve_signal_stop_context() -> (
    tuple[dict[str, object], dict[str, object], str, str] | None
):
    """Read and validate the Stop-hook payload + cw-context.json.

    Returns ``(hook_payload, context, cwd_value, cw_session_id)`` when every
    required field is present and well-typed, else ``None`` (silent no-op so
    hook execution never blocks claude from exiting). See :func:`signal_stop`.
    """
    payload = _read_stop_hook_payload()
    if payload is None:
        return None
    hook_payload, cwd_value = payload

    context_path = Path(cwd_value) / ".claude" / "cw-context.json"
    if not context_path.is_file():
        return None
    try:
        context = json.loads(context_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(context, dict):
        return None

    cw_session_id = context.get("session_id")
    if not isinstance(cw_session_id, str):
        return None

    return hook_payload, context, cwd_value, cw_session_id


def _handle_headless_no_sentinel(
    state: CwState,
    session: Session,
    *,
    now: datetime,
    claude_session_id: object,
    context: dict[str, object],
    ticket_id_value: object,
    hook_payload: dict[str, object],
) -> bool:
    """Resolve a sentinel-less headless Stop hook: defer or time out.

    Under the resolved headless budget the call defers (returns True without
    mutating state) so a later Stop hook or reconcile can retry. Over budget
    it records a TIMED_OUT transition via :func:`_record_headless_timeout`.
    Returns True in both cases — the caller must stop processing.
    """
    elapsed = (now - session.started_at).total_seconds()
    headless_config = load_orchestrator_config()
    stop_task: TicketTask | None = None
    if isinstance(ticket_id_value, str):
        stop_store = load_dev_queue()
        stop_task = next(
            (t for t in stop_store.tasks if t.ticket_id == ticket_id_value),
            None,
        )
    budget = resolve_headless_budget(stop_task, session, headless_config)
    if elapsed < budget:
        # Under budget — defer. Another Stop hook turn will fire, or
        # reconcile will eventually catch a phantom and CRASH it.
        return True
    # Budget exceeded without sentinel → TIMED_OUT (loud, retry-eligible).
    _record_headless_timeout(
        state,
        session,
        now=now,
        elapsed=elapsed,
        claude_session_id=claude_session_id,
        context=context,
        ticket_id_value=ticket_id_value,
        hook_payload=hook_payload,
    )
    return True


def _handle_user_origin_stop(
    state: CwState,
    session: Session,
    claude_session_id: object,
) -> bool:
    """Handle a Stop hook for a USER-origin (interactive) session.

    Issue #165 Phase B: mark an ACTIVE session IDLE (no SESSION_COMPLETED,
    no daemon stop). A non-ACTIVE session is left untouched. Returns True
    when the caller should stop processing (always, for USER origin).
    """
    if session.status != SessionStatus.ACTIVE:
        # BACKGROUNDED (or any non-ACTIVE state) — silent no-op so a
        # Stop hook firing on a session the user has explicitly
        # parked doesn't flip its status.
        return True
    session.status = SessionStatus.IDLE
    if isinstance(claude_session_id, str):
        session.claude_session_id = claude_session_id
    save_state(state)
    return True


def _record_headless_timeout(
    state: CwState,
    session: Session,
    *,
    now: datetime,
    elapsed: float,
    claude_session_id: object,
    context: dict[str, object],
    ticket_id_value: object,
    hook_payload: dict[str, object],
) -> None:
    """Mark a budget-exceeded headless session TIMED_OUT and revert its task.

    Transitions *session* to TIMED_OUT, persists state, emits
    ``SESSION_TIMED_OUT``, reverts the owning RUNNING TicketTask to PENDING so
    the dispatch loop can retry, and best-effort stops the daemon worker.
    See issue #176.
    """
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
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                break
        save_dev_queue(store)
    if session.surface_ref is not None:
        get_native_daemon_client().stop(session.surface_ref)


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
    resolved_context = _resolve_signal_stop_context()
    if resolved_context is None:
        return
    hook_payload, context, cwd_value, cw_session_id = resolved_context

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

    # Why not mutate_state: dual-lock (dev_queue_lock nested at the TIMED_OUT path)
    # and daemon.stop() network call inside the lock window (criteria 1 and 2).
    with sessions_lock():
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
            _handle_user_origin_stop(state, session, claude_session_id)
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
                cwd_value,
                claude_session_id if isinstance(claude_session_id, str) else None,
            )
            # Issue #799: when EnterWorktree shifts the hook cwd to a nested
            # worktree, cwd_value derives the wrong Claude project dir. Retry
            # with the session's recorded worktree_path — the directory whose
            # project dir holds the actual transcript.
            if parsed_sentinel is None and session.worktree_path is not None:
                parsed_sentinel = _parse_sentinel_from_transcript(
                    str(session.worktree_path),
                    claude_session_id if isinstance(claude_session_id, str) else None,
                )
            if parsed_sentinel is None:
                _handle_headless_no_sentinel(
                    state,
                    session,
                    now=now,
                    claude_session_id=claude_session_id,
                    context=context,
                    ticket_id_value=ticket_id_value,
                    hook_payload=hook_payload,
                )
                return

        # Issue #251: directly update the dev-queue task *before* marking the
        # session COMPLETED. This closes the race where revert_completed_silent_tasks
        # sees a COMPLETED session with a still-RUNNING task and reverts it to
        # PENDING before consume_completed_sessions can process the event — causing
        # no_op and similar terminal outcomes to trigger infinite re-dispatch.
        if (
            is_headless
            and parsed_sentinel is not None
            and isinstance(ticket_id_value, str)
        ):
            _apply_sentinel_to_task(ticket_id_value, session.id, parsed_sentinel)

        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        if isinstance(claude_session_id, str):
            session.claude_session_id = claude_session_id
        # Issue #225: headless DAEMON sessions set last_result via signal_stop,
        # which parses the transcript before save_state so downstream consumers
        # (consume_completed_sessions, /cw-followup) can route by status.
        # parse_stdout returns BlockedResult on malformed payloads — we
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
