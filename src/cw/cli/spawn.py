"""Daemon-spawn lifecycle commands (``spawn`` group: create/close/complete)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast, get_args

import click

from cw._flock import (
    OPERATOR_COMMAND_LOCK_WAIT_S,
    SESSIONS_LOCK_TIMEOUT_ENV,
    operator_close_lock_wait_seconds,
)
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
    dev_queue_lock,
    load_dev_queue,
    requeue_ticket,
)
from cw.dispatch import _DISPATCH_CONSUMER, _apply_events_to_store
from cw.events import advance_cursor, record_event
from cw.exceptions import CwError, RequeueStateError, SessionsLockTimeoutError
from cw.models import (
    ClientConfig,
    CompletionReason,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client
from cw.reconcile import ticket_id_for_session
from cw.reconcile.session_close import close_session_in_state, queue_close
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from cw.models import Session

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
    surface_already_stopped: bool = False,
    lock_timeout_s: float | None = None,
) -> None:
    """Close a daemon-spawned session.

    DAEMON-origin sessions are stopped via the native daemon client.
    USER-origin sessions with a legacy ``surface_ref`` are logged and
    skipped — the multiplexer adapter has been removed. Separated from
    the Click command so tests can inject the daemon client directly.

    ``surface_already_stopped`` (#2517): the caller already stopped the
    worker and confirmed it left the roster (or proved there is none), so the
    post-lock stop is skipped -- no second stop, no client resolved. The
    cancel/route step and the COMPLETED stamp are unchanged.

    Raise-or-flip (#2694): a held sessions lock raises
    :class:`~cw.exceptions.SessionsLockTimeoutError` after *lock_timeout_s*
    (``None``: the env-or-60 s default) and records nothing. Only the
    ``spawn close`` command queues the close; ``routed_orphan_close`` relies
    on this raising.
    """
    surface_to_stop: str | None = None
    # Why not mutate_state: cancel_task_for_session / route_staged_emit_result
    # take dev_queue_lock inside the sessions_lock window (criterion 2: no
    # dual-lock).
    # bounded=True (#2491): operator recovery command with no side effect
    # before the lock, so a held lock must fail fast, not hang the recovery.
    with sessions_lock(bounded=True, timeout_s=lock_timeout_s):
        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        if sess is None:
            msg = f"Session '{session_id}' not found."
            raise CwError(msg)
        prior_status = sess.status
        # The close body (#2480 leaked-surface stop, #317 row cancel, #2458
        # staged-result routing) lives in cw.reconcile.session_close so a
        # queued close applied by reconcile runs the same code.
        surface_to_stop = close_session_in_state(sess)
        if sess.status != prior_status:
            save_state(state)

    # daemon.stop is best-effort and slow (up to 10s), so it runs after the lock
    # releases (#2547, mirrors signal_stop / done_session). A raise leaves the
    # state already stamped; a repeat close retries just the stop (#2480).
    if surface_to_stop is not None and not surface_already_stopped:
        (native_daemon or get_native_daemon_client()).stop(surface_to_stop)


def _spawn_close_requeue_impl(
    *,
    session_id: str,
    ticket_id: str | None,
    client: str | None,
    closed_session_id: str | None = None,
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

    *closed_session_id* (the resolved ``Session.id``) is exempted from
    ``requeue_ticket``'s live-session guard (#2275), since its roster entry
    may lag the close. Any *other* live session for the ticket still raises
    ``RequeueLiveSessionError``, which propagates to ``handle_errors`` -- as
    does its ``RequeueRosterUnreadableError`` subclass: an unreadable roster
    cannot rule out a live session the exemption does not name, so the close
    lands but the requeue is refused.
    """
    if ticket_id is None or client is None:
        click.echo(
            f"--requeue: no ticket_id resolved from session '{session_id}';"
            " --requeue is a no-op."
        )
        return

    try:
        result = requeue_ticket(
            ticket_id,
            client,
            allow_regress=False,
            from_cancelled=True,
            ignore_session_ids=(
                frozenset({closed_session_id}) if closed_session_id else frozenset()
            ),
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
        "Mark the session as headless in cw-context.json: the Stop hook then "
        "requires an AUTO_DEV_RESULT sentinel before it marks the session "
        "COMPLETED, and defers indefinitely without one (ADR-0014: there is no "
        "wall-clock budget and no timeout transition). Use only when the "
        "prompt invokes /auto-dev --headless or another skill that emits the "
        "sentinel contract; omit it for a prompt that emits no sentinel, or "
        "the session never reaches a terminal state."
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
        "`cw dev-queue show <T> -c <CLIENT>` / event history first if the "
        "ticket may have been deliberately cancelled. Redundant with the "
        "cancelled_row_restore "
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

    Waits at most 5s for the sessions lock (less if CW_SESSIONS_LOCK_TIMEOUT_S
    is lower). If another cw process still holds it, the close is QUEUED
    instead: it is recorded, applied by the next reconcile pass (the serve
    tick, or any cw list/status), and the command exits 1 with
    "QUEUED, NOT YET CLOSED" and the entry path to delete to withdraw it.

    \b
    Example:
      cw spawn close abc12345
      cw spawn close --confirmed-dead abc12345  # flag first for allowlist match
      cw spawn close --confirmed-dead --requeue abc12345  # + requeue its ticket
    """
    del confirmed_dead  # permission-prefix token only; not forwarded to impl

    # Read-only per ADR-0005, lock-free: resolve the session once *before*
    # _spawn_close_impl runs, so the requeue orchestration below has
    # ticket_id/client without re-deriving them from a session that's already
    # COMPLETED, and a lock timeout can queue the close against it (#2694).
    sess = load_state().find_by_name_or_id(session_id)
    ticket_id: str | None = None
    client: str | None = None
    closed_session_id: str | None = None
    if requeue and sess is not None:
        ticket_id = ticket_id_for_session(sess.name)
        client = sess.client
        closed_session_id = sess.id

    try:
        _spawn_close_impl(
            session_id=session_id, lock_timeout_s=operator_close_lock_wait_seconds()
        )
    except SessionsLockTimeoutError as exc:
        _queue_close_or_reraise(
            exc,
            session_id=session_id,
            sess=sess,
            requeue_hint=_requeue_hint(requeue, ticket_id, client),
        )
    click.echo(f"Closed session: {session_id}")

    if requeue:
        _spawn_close_requeue_impl(
            session_id=session_id,
            ticket_id=ticket_id,
            client=client,
            closed_session_id=closed_session_id,
        )


def _requeue_hint(requeue: bool, ticket_id: str | None, client: str | None) -> str:
    """The ``--requeue`` follow-up for a queued close (the row is still RUNNING)."""
    if not requeue:
        return ""
    if ticket_id is None or client is None:
        return " --requeue was not run, and no ticket_id resolves from this session."
    return (
        " --requeue was not run (the ticket's row is still RUNNING); once the"
        f" close has applied, run: cw dev-queue requeue {ticket_id} -c {client}"
        " --from-cancelled."
    )


def _queue_close_or_reraise(
    exc: SessionsLockTimeoutError,
    *,
    session_id: str,
    sess: Session | None,
    requeue_hint: str,
) -> NoReturn:
    """Record the close for the next reconcile pass, then fail with exit 1 (#2694).

    An unknown id is reported as not found (nothing to queue). If the entry
    cannot be written the original timeout error propagates unchanged. The
    raised message leads with ``QUEUED, NOT YET CLOSED`` and carries the
    timeout text once, so ``cw spawn close X && <next step>`` stops here.
    """
    if sess is None:
        msg = f"Session '{session_id}' not found."
        raise CwError(msg) from exc
    try:
        entry_path = queue_close(sess, origin="spawn_close", now=datetime.now(UTC))
    except (OSError, ValueError):
        logger.warning("spawn_close_queue_failed: session=%s", sess.id, exc_info=True)
        raise exc from None
    lead = (
        f"QUEUED, NOT YET CLOSED: session {sess.id} is still {sess.status.value}."
        " The close is recorded and the next reconcile pass (the serve tick, or"
        " any cw list/status) applies it; that is not confirmed while serve"
        " itself is the wedged holder, so restart serve if dispatch.tick has"
        f" stopped. cw spawn close waits at most {OPERATOR_COMMAND_LOCK_WAIT_S:g}s"
        f" whatever {SESSIONS_LOCK_TIMEOUT_ENV} says; re-running it is safe. To"
        f" withdraw, delete {entry_path}.{requeue_hint}"
    )
    msg = f"{lead}\n{exc}"
    raise CwError(msg) from exc


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
    event: OrchestratorEvent | None = None
    # Why not mutate_state: dev_queue_lock nested inside the sessions_lock
    # window (criterion 2: no dual-lock).
    # bounded=True (#2491): operator recovery command; the lock is taken before
    # any event is recorded or queue write, so a timeout is a clean retry.
    with sessions_lock(bounded=True):
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
            # --force no-op: falls through to the shared post-lock stop (#2480).
        else:
            effective_ticket_id = ticket_id or ticket_id_for_session(sess.name)

            payload: dict[str, object] = {
                "session_id": sess.id,
                "client": sess.client,
                "crashed": False,
                "status": status,
                **({"ticket_id": effective_ticket_id} if effective_ticket_id else {}),
            }

            # bounded=True (#2501): taken before the event is recorded or the
            # queue written, so a timeout is a clean retry. It holds
            # sessions_lock while it waits (each bounded lock waits up to the
            # timeout on its own).
            with dev_queue_lock(bounded=True):
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

                # Step 1: Record event (the leaf inbox lock nests here, ADR-0019)
                event = record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

                # Step 2: Apply to queue
                _apply_events_to_store(store, [event], clients=load_effective_clients())

                # Step 3: Close session state
                sess.status = SessionStatus.COMPLETED
                sess.completed_at = datetime.now(UTC)
                if sess.completed_reason is None:
                    sess.completed_reason = CompletionReason.USER
                save_state(state)

    # Advance cursor after the lock (advance_cursor takes the leaf inbox lock)
    if event is not None:
        advance_cursor(_DISPATCH_CONSUMER, event.id)

    # daemon.stop outside lock — best-effort, slow (up to 10s timeout)
    if sess.surface_ref is not None and sess.origin is SessionOrigin.DAEMON:
        (native_daemon or get_native_daemon_client()).stop(sess.surface_ref)


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
