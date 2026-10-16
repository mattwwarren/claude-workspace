"""Daemon-spawn lifecycle commands (``spawn`` group: create/close/complete)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast, get_args

import click

from cw._flock import operator_close_lock_wait_seconds
from cw.auto_dev_result import Status
from cw.cli._base import handle_errors, main
from cw.config import (
    get_client,
    load_effective_clients,
    load_state,
    mutate_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import (
    _find_ticket,
    cancel_task_for_session,
    dev_queue_lock,
    load_dev_queue,
    requeue_ticket,
    running_task_for_session,
)
from cw.dispatch import _DISPATCH_CONSUMER, _apply_events_to_store
from cw.events import advance_cursor, record_event
from cw.exceptions import CwError, RequeueStateError, SessionsLockTimeoutError
from cw.history import EventType, HistoryEvent
from cw.history import record_event as record_history_event
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    ClientConfig,
    CompletionReason,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import (
    NativeDaemonClient,
    get_native_daemon_client,
    wait_for_roster_presence,
)
from cw.reconcile import (
    _apply_sentinel_to_task,
    holds_staged_emit_result,
    ticket_id_for_session,
)
from cw.result import reconstruct_staged_sentinel
from cw.spawn import spawn_create_impl

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from cw.models import CwState, Session

logger = logging.getLogger(__name__)

# ``detail`` of the one history record every operator session close leaves
# (#2694): ``cw spawn close`` and the ``cw dev-queue cancel`` session step.
OPERATOR_CLOSE_DETAIL = "operator_close"
_SPAWN_CLOSE_COMMAND = "cw spawn close"
_CANCEL_COMMAND = "cw dev-queue cancel"
# How long the cancel session step polls the daemon roster for a stopped
# worker to leave before it refuses to mark the session completed (#2694);
# the routed-orphan close's confirmation window.
_CANCEL_STOP_CONFIRM_TIMEOUT_S = 5.0
_CANCEL_STOP_CONFIRM_INTERVAL_S = 0.5


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


def _route_staged_emit_result(sess: Session) -> bool:
    """Route *sess*'s staged ``cw result emit`` result to its RUNNING row (#2458).

    Returns True when the row is now dispositioned by that result -- routed,
    or already landed terminal by a concurrent caller -- so the caller must
    NOT also cancel it. Returns False (cancel as before) when nothing is
    staged, the staged dict reconstructs into neither sentinel arm, the
    session owns no RUNNING row, or the shared authority refuses the route
    (a #1031 stage mismatch, or a BlockedResult landing the row FAILED).

    The row is found by :func:`~cw.dev_queue.running_task_for_session` --
    the lookup ``cancel_task_for_session`` itself uses -- via a lock-free
    read; ``_apply_sentinel_to_task`` re-resolves it under
    ``dev_queue_lock``. Called under ``sessions_lock``, the same
    ``sessions_lock`` -> ``dev_queue_lock`` nesting ``signal_stop`` already
    uses.
    """
    if not holds_staged_emit_result(sess):
        return False
    staged = reconstruct_staged_sentinel(sess.last_result)
    if staged is None:
        return False
    task = running_task_for_session(load_dev_queue(), sess.id)
    if task is None:
        return False
    outcome = _apply_sentinel_to_task(task.ticket_id, sess, staged)
    return outcome.routed or outcome.task_already_terminal


def record_operator_close(
    sess: Session, *, command: str, prior_status: SessionStatus
) -> None:
    """Record the one durable history event of an operator close (#2694).

    ADR-0006 invariant 3: a ``session_completed`` record (``detail`` is
    :data:`OPERATOR_CLOSE_DETAIL`) with the session's identity, the operator
    *command* and *prior_status*, written right after the close is saved. A
    write failure raises :class:`~cw.exceptions.CwError`; the close stands.
    """
    metadata = {
        "actor": "operator",
        "command": command,
        "session_id": sess.id,
        "ticket_id": ticket_id_for_session(sess.name) or "",
        "surface_ref": sess.surface_ref or "",
        "claude_session_id": sess.claude_session_id or "",
        "started_at": sess.started_at.isoformat(),
        "prior_status": prior_status.value,
        "resulting_status": sess.status.value,
    }
    event = HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client=sess.client,
        session_id=sess.id,
        session_name=sess.name,
        purpose=sess.purpose,
        detail=OPERATOR_CLOSE_DETAIL,
        metadata=metadata,
    )
    try:
        record_history_event(sess.client, event)
    except (OSError, ValueError) as exc:
        logger.exception(
            "operator_close_audit_failed: session_id=%s command=%s", sess.id, command
        )
        msg = (
            f"Session {sess.id} was closed, but its audit record could not be"
            f" written: {exc}"
        )
        raise CwError(msg) from exc


def _spawn_close_impl(
    *,
    session_id: str,
    native_daemon: NativeDaemonClient | None = None,
    surface_already_stopped: bool = False,
    lock_timeout_s: float | None = None,
    audit_command: str | None = None,
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

    *lock_timeout_s* (#2694) bounds the sessions-lock wait (``None``: the
    env-or-60 s default); a held lock raises
    :class:`~cw.exceptions.SessionsLockTimeoutError` naming the recorded
    holder and nothing is closed. *audit_command* names the operator command
    whose close is audited by :func:`record_operator_close` after the save;
    ``None`` records nothing (the routed-orphan close audits itself).
    """
    surface_to_stop: str | None = None
    closed: tuple[Session, SessionStatus] | None = None
    # Why not mutate_state: cancel_task_for_session / _route_staged_emit_result
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
        if sess.status == SessionStatus.COMPLETED:
            # #2480: an already-completed session's daemon surface may still be
            # sitting live in the roster (the Stop hook's rescued=None bail, or
            # any other completion path that never called daemon.stop()) --
            # occupying the ticket's worktree forever and blocking
            # re-dispatch. Stop it (idempotent: NativeDaemonClient.stop()
            # swallows a missing/already-gone surface) instead of refusing the
            # close outright, so a second `cw spawn close` genuinely clears the
            # leak rather than erroring on the very case it exists to fix.
            if sess.surface_ref is not None and sess.origin is SessionOrigin.DAEMON:
                surface_to_stop = sess.surface_ref
        else:
            if sess.surface_ref is not None:
                if sess.origin is SessionOrigin.DAEMON:
                    surface_to_stop = sess.surface_ref
                else:
                    logging.getLogger(__name__).warning(
                        "Session %s has legacy surface_ref %r; skipping surface close",
                        sess.id,
                        sess.surface_ref,
                    )

            # For DAEMON sessions, atomically cancel any RUNNING TicketTask that
            # owns this session so revert_completed_silent_tasks cannot revert it
            # to PENDING and the dispatcher cannot re-spawn the same ticket in
            # the same tick. (See GitHub issue #317.) A worker that already
            # emitted its result has that result routed first instead (#2458) --
            # cancelling would throw away validated work and force a manual
            # `requeue --from-cancelled`.
            if sess.origin is SessionOrigin.DAEMON and not _route_staged_emit_result(
                sess
            ):
                cancel_task_for_session(sess.id)

            closed = (sess, sess.status)
            sess.status = SessionStatus.COMPLETED
            sess.completed_at = datetime.now(UTC)
            sess.completed_reason = CompletionReason.USER
            save_state(state)

    # daemon.stop is best-effort and slow (up to 10s), so it runs after the lock
    # releases (#2547, mirrors signal_stop / done_session). A raise leaves the
    # state already stamped; a repeat close retries just the stop (#2480). The
    # audit record is written first; a failed one still lets the stop run.
    try:
        if closed is not None and audit_command is not None:
            record_operator_close(
                closed[0], command=audit_command, prior_status=closed[1]
            )
    finally:
        if surface_to_stop is not None and not surface_already_stopped:
            (native_daemon or get_native_daemon_client()).stop(surface_to_stop)


class CancelCloseOutcome(NamedTuple):
    """What the ``cw dev-queue cancel`` session steps left unfinished (#2694).

    Accumulated across one command's tickets: *problems* holds one
    operator-facing line per session not marked completed; *lock_timed_out*
    means a later ticket tries the sessions lock once without waiting.
    """

    problems: tuple[str, ...] = ()
    lock_timed_out: bool = False

    def report(self) -> None:
        """Echo every problem to stderr (before another error ends the command)."""
        for problem in self.problems:
            click.echo(problem, err=True)

    def raise_if_unfinished(self) -> None:
        """Fail the command (exit 1) naming every unfinished session."""
        if self.problems:
            raise CwError("\n".join(self.problems))


def _incarnation(sess: Session) -> tuple[object, ...]:
    """What identifies one run of *sess*; a resume changes it (#2694)."""
    return (sess.started_at, sess.resumed_at, sess.surface_ref)


def _stop_until_gone(sess: Session, daemon: NativeDaemonClient) -> str | None:
    """Stop *sess*'s worker and confirm it left the roster; the problem if not.

    The confirmation is the shared roster poll, read fail closed: a worker
    still listed, or a roster that cannot be read, never confirms. A terminal
    session's worker (a leaked one) is stopped without waiting, since it is
    not flipped. A session with no surface has no worker to stop.
    """
    if sess.surface_ref is None:
        return None
    daemon.stop(sess.surface_ref)
    if sess.status in TERMINAL_SESSION_STATUSES or wait_for_roster_presence(
        daemon,
        sess.surface_ref,
        present=False,
        timeout=_CANCEL_STOP_CONFIRM_TIMEOUT_S,
        interval=_CANCEL_STOP_CONFIRM_INTERVAL_S,
    ):
        return None
    return (
        f"Session {sess.id}: its worker {sess.surface_ref} is still in the daemon"
        " roster (or the roster is unreadable) after the stop, so the session was"
        f" not marked completed; run `cw spawn close {sess.id}` once it is gone."
    )


def _flip_cancelled(
    read: Mapping[str, Session], timeout_s: float
) -> tuple[list[tuple[Session, SessionStatus]], list[str]]:
    """Flip the *read* sessions COMPLETED(USER); return (flipped, resumed ids).

    Flip-only: the row is already CANCELLED, so no dev-queue lock is taken
    inside the sessions-lock hold. A session whose incarnation is no longer
    the one *read* (resumed since) is left as is; one already terminal or
    gone is skipped.
    """
    flipped: list[tuple[Session, SessionStatus]] = []
    resumed: list[str] = []

    def _flip(state: CwState) -> None:
        for sess in state.sessions:
            seen = read.get(sess.id)
            if seen is None or sess.status in TERMINAL_SESSION_STATUSES:
                continue
            if _incarnation(sess) != _incarnation(seen):
                resumed.append(sess.id)
                continue
            flipped.append((sess, sess.status))
            sess.status = SessionStatus.COMPLETED
            sess.completed_at = datetime.now(UTC)
            sess.completed_reason = CompletionReason.USER

    # bounded=True (#2694): the `cw dev-queue cancel` session step. Its row
    # is already CANCELLED and its worker stopped and confirmed gone, so a
    # timeout leaves only the flip pending, which the caller reports.
    mutate_state(_flip, bounded=True, timeout_s=timeout_s)
    return flipped, resumed


def close_cancelled_sessions(
    session_ids: Iterable[str | None],
    daemon: NativeDaemonClient,
    prior: CancelCloseOutcome,
) -> CancelCloseOutcome:
    """Stop, confirm gone, then mark completed each session a cancel unbound.

    *session_ids* are the ids ``cancel_ticket`` cleared from the cancelled
    row: the sessions bound to it at cancel time (#2694). Each DAEMON session
    is read lock-free and its worker stopped and confirmed gone; only then is
    it flipped COMPLETED(USER) under one sessions-lock wait of
    :func:`~cw._flock.operator_close_lock_wait_seconds` (none once *prior*
    timed out), re-checked against the incarnation that was read, and
    audited once (:func:`record_operator_close`). USER-origin and unknown ids
    are untouched. A sessions-lock timeout leaves the stopped sessions
    unflipped and reports the pending flip; nothing is queued. Returns
    *prior* extended with this ticket's problems.
    """
    wanted = {sid for sid in session_ids if sid is not None}
    read = {
        s.id: s
        for s in (load_state().sessions if wanted else ())
        if s.id in wanted and s.origin is SessionOrigin.DAEMON
    }
    problems = list(prior.problems)
    to_flip: dict[str, Session] = {}
    for sess in read.values():
        problem = _stop_until_gone(sess, daemon)
        if problem is not None:
            problems.append(problem)
        elif sess.status not in TERMINAL_SESSION_STATUSES:
            to_flip[sess.id] = sess
    if not to_flip:
        return prior._replace(problems=tuple(problems))
    wait_s = 0.0 if prior.lock_timed_out else operator_close_lock_wait_seconds()
    try:
        flipped, resumed = _flip_cancelled(to_flip, wait_s)
    except SessionsLockTimeoutError as exc:
        problems.extend(
            f"Session {sid}: the worker was stopped, but the record flip is"
            " pending (the sessions lock is busy, so it is not marked completed"
            f" yet); run `cw spawn close {sid}` or `cw doctor` once it frees."
            for sid in to_flip
        )
        problems.append(str(exc))
        return CancelCloseOutcome(tuple(problems), lock_timed_out=True)
    problems.extend(
        f"Session {sid} was resumed into a new incarnation after the cancel read"
        " it, so it was left as is (not marked completed); run"
        f" `cw spawn close {sid}` if it should not run."
        for sid in resumed
    )
    for sess, prior_status in flipped:
        try:
            record_operator_close(
                sess, command=_CANCEL_COMMAND, prior_status=prior_status
            )
        except CwError as exc:
            problems.append(str(exc))
    return prior._replace(problems=tuple(problems))


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
    is lower); if another cw process still holds it, exits 1 naming the
    recorded holder and closes nothing. Re-run it once the holder is gone.

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
    closed_session_id: str | None = None
    if requeue:
        state = load_state()
        sess = state.find_by_name_or_id(session_id)
        if sess is not None:
            ticket_id = ticket_id_for_session(sess.name)
            client = sess.client
            closed_session_id = sess.id

    _spawn_close_impl(
        session_id=session_id,
        lock_timeout_s=operator_close_lock_wait_seconds(),
        audit_command=_SPAWN_CLOSE_COMMAND,
    )
    click.echo(f"Closed session: {session_id}")

    if requeue:
        _spawn_close_requeue_impl(
            session_id=session_id,
            ticket_id=ticket_id,
            client=client,
            closed_session_id=closed_session_id,
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
