"""Daemon-spawn lifecycle commands (``spawn`` group: create/close/complete)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args

import click

from cw.auto_dev_result import Status
from cw.cli._base import handle_errors, main
from cw.config import (
    get_client,
    load_effective_clients,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import (
    _find_ticket,
    cancel_task_for_session,
    dev_queue_lock,
    load_dev_queue,
    requeue_ticket,
)
from cw.dispatch import _DISPATCH_CONSUMER, _apply_events_to_store
from cw.events import advance_cursor, record_event
from cw.exceptions import CwError, RequeueStateError
from cw.models import (
    ClientConfig,
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client
from cw.reconcile import ticket_id_for_session
from cw.spawn import spawn_create_impl

logger = logging.getLogger(__name__)


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
    # Why not mutate_state: daemon.stop() network call inside the lock window
    # (criterion 1: no subprocess/network in lock).
    with sessions_lock():
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


def _spawn_close_requeue_impl(
    *,
    session_id: str,
    ticket_id: str | None,
    client: str | None,
) -> None:
    """Requeue a just-closed session's ticket back to PENDING (``--requeue``).

    Folds ``cw dev-queue requeue ... --from-cancelled`` into ``cw spawn
    close``. No-ops with a message if no ticket_id resolves from the session
    name. Handles the cancelled_row_restore concierge race (#1889): a
    concierge tick may have already landed the row on PENDING/RUNNING in the
    window between :func:`_spawn_close_impl`'s cancel and the
    :func:`requeue_ticket` call below — one fresh read tells us whether
    that's what happened, or whether this is a genuine state problem that
    should propagate. Separated from the Click command so tests can call it
    directly.
    """
    if ticket_id is None or client is None:
        click.echo(
            f"--requeue: no ticket_id resolved from session '{session_id}';"
            " --requeue is a no-op."
        )
        return

    try:
        result = requeue_ticket(
            ticket_id, client, allow_regress=False, from_cancelled=True
        )
    except RequeueStateError:
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client)
        if task.status in (QueueItemStatus.PENDING, QueueItemStatus.RUNNING):
            logger.info(
                "spawn_close_requeue_race_resolved: ticket_id=%s client=%s status=%s",
                ticket_id,
                client,
                task.status.value,
            )
            click.echo(
                f"Ticket '{ticket_id}' already {task.status.value};"
                " --requeue is a no-op."
            )
            return
        raise
    else:
        record_event(
            OrchestratorEventType.TICKET_REQUEUED,
            {
                "ticket_id": ticket_id,
                "client": client,
                "from_stage": result["from_stage"],
                "to_stage": result["to_stage"],
                "reason": "spawn_close_requeue",
                "regressed": result["regressed"],
            },
        )
        click.echo(
            f"Requeued {ticket_id} ({client}):"
            f" {result['from_stage']} -> {result['to_stage']} (PENDING)"
        )


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
@click.option(
    "--confirmed-dead",
    is_flag=True,
    default=False,
    help=(
        "Assert the session is already terminal (no process, no worktree "
        "activity, no recent events). Purely a permission-system marker — it "
        "changes no behavior. Place it BEFORE the session id "
        "(cw spawn close --confirmed-dead <id>) so an allowlist rule like "
        "Bash(cw spawn close --confirmed-dead*) can match while a bare "
        "cw spawn close <id> stays gated by the auto-mode classifier."
    ),
)
@click.option(
    "--requeue",
    is_flag=True,
    default=False,
    help=(
        "After closing, also requeue the session's ticket back to PENDING at "
        "its current stage (folds `cw dev-queue requeue ... --from-cancelled` "
        "into this one command). No-ops with a message if no ticket_id "
        "resolves from the session name. Same caveat as --from-cancelled: "
        "accepts a CANCELLED row regardless of why it was cancelled -- check "
        "`cw dev-queue show` / event history first if the ticket may have "
        "been deliberately cancelled. Redundant with the cancelled_row_restore "
        "concierge recipe when concierge_enabled: true and the worktree has "
        "committed work ahead of base (see docs/dispatch-runbook.md §7/§11.1) "
        "-- safe to pass either way, since a concierge tick that already "
        "landed the row on PENDING/RUNNING first is treated as success here, "
        "not an error."
    ),
)
@handle_errors
def spawn_close(session_id: str, confirmed_dead: bool, requeue: bool) -> None:
    """Close a spawned session by session ID.

    Stops the session via the native daemon and marks it as COMPLETED.

    \b
    Example:
      cw spawn close abc12345
      cw spawn close --confirmed-dead abc12345  # flag first for allowlist match
      cw spawn close --confirmed-dead --requeue abc12345  # + requeue its ticket
    """
    del confirmed_dead  # permission-prefix token only; not forwarded to impl

    # Read-only per ADR-0005: resolve ticket_id/client from state *before*
    # _spawn_close_impl runs, so the requeue orchestration below has what it
    # needs without re-deriving it from a session that's already COMPLETED.
    ticket_id: str | None = None
    client: str | None = None
    if requeue:
        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        if sess is not None:
            ticket_id = ticket_id_for_session(sess.name)
            client = sess.client

    _spawn_close_impl(session_id=session_id)
    click.echo(f"Closed session: {session_id}")

    if requeue:
        _spawn_close_requeue_impl(
            session_id=session_id, ticket_id=ticket_id, client=client
        )


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
    # Why not mutate_state: dev_queue_lock nested inside the sessions_lock
    # window (criterion 2: no dual-lock).
    with sessions_lock():
        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        if sess is None:
            msg = f"Session '{session_id}' not found."
            raise CwError(msg)

        if sess.status == SessionStatus.COMPLETED:
            if not force:
                msg = (
                    f"Session '{session_id}' is already completed."
                    " Use --force to no-op."
                )
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
                            f"Queue task for '{effective_ticket_id}'"
                            " is already COMPLETED."
                            " Use 'cw spawn close' to close the session only."
                        )
                        raise CwError(already_done_task_msg)

            # Step 1: Record event (record_event uses _inbox_lock — no deadlock risk)
            event = record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

            # Step 2: Apply to queue
            _apply_events_to_store(store, [event], clients=load_effective_clients())

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
