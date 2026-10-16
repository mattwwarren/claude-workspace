"""Close an orphaned routed-result session from ``approve`` / ``requeue`` (#2517).

A #2458 partial route (a Stop whose background work is still running) routes
the session's result but leaves the session ACTIVE. Once ``cw dev-queue
approve`` or ``requeue`` clears the row's ``session_id``, nothing completes
that session, and dispatch defers the ticket as ``worktree_occupied``
indefinitely. :func:`close_routed_result_sessions_for_ticket` is the operator
CLI's close for exactly that shape: the explicit operator command is the
authority (ADR-0014 invariant 2), so it runs from ``cw dev-queue approve``
(plain and ``--scope-drift``, after the transition) and ``cw dev-queue
requeue`` (before ``requeue_ticket``) only -- never from an unattended path.

Each marker candidate that no occupied row pins and whose background work is
not draining is stopped, confirmed gone from the daemon roster, and only then
flipped COMPLETED through ``cw.cli.spawn._spawn_close_impl`` (with
``surface_already_stopped=True``). A candidate absent from a readable,
trustworthy roster is flipped without a stop. Everything fails closed: an
unconfirmed stop, or a roster that cannot be read or looks like a daemon
restart, never flips a session or releases its worktree.

Split out of ``cw.cli.spawn`` to keep that module under the ~1000-line
ceiling. It holds no lock and never takes ``sessions_lock`` itself (ADR-0019):
the daemon stop and the roster polls run lock-free, and ``_spawn_close_impl``
takes ``sessions_lock`` for the status flip only.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

import click

from cw.cli.spawn import _spawn_close_impl
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.doctor.routed_result_wedge import (
    _emit_audit_record,
    _finalize_audit_intent,
    _queue_audit_intents,
)
from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent, record_event
from cw.models import DEFAULT_LANE, DEFAULT_STAGE, SessionStatus
from cw.native_daemon import get_native_daemon_client, wait_for_roster_presence
from cw.reconcile.liveness_page import close_command
from cw.reconcile.routed_result_sessions import (
    StrandedRoutedSession,
    _roster_outage_shaped,
    routed_marker_candidates,
    split_resolvable_routed_sessions,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorConfig, Session
    from cw.native_daemon import NativeDaemonClient
    from cw.reconcile.routed_result_sessions import (
        PinnedOrphan,
        RoutedOrphanSplit,
    )

logger = logging.getLogger(__name__)

# Stop-confirmation window. Same values as reconcile's mid-turn usage-limit
# sweep (cw.reconcile.usage_limit_mid_turn), which keeps them short because it
# holds sessions_lock; this helper holds no lock, so that rationale does not
# apply. Kept after the Deferred Premise 1 probe (claude 2.1.293): `claude
# stop` dropped the roster entry within ~340ms, both for a trivial session and
# for a host whose background child outlived the stop. A window that is too
# short only makes the command refuse (fail closed; `claude stop` is
# idempotent and the retry closes by a status flip alone); it never closes a
# live worker. The interval also spaces the two roster reads that must agree
# before a flip-only close.
_ROUTED_STOP_CONFIRM_TIMEOUT_SECS = 5.0
_ROUTED_STOP_CONFIRM_INTERVAL_SECS = 0.5

# Reason tokens of the routed_orphan_* WARNING lines (the R8 table).
_REASON_ROSTER_UNREADABLE = "roster_unreadable"
_REASON_PINNED = "pinned"
_REASON_NO_LONGER_CANDIDATE = "no_longer_candidate"
_REASON_UNREADABLE_BEFORE_STOP = "roster_unreadable_before_stop"
_REASON_DRAINING = "background_work_draining"
_REASON_STILL_LISTED = "worker_still_listed"
_REASON_UNREADABLE_AFTER_STOP = "roster_unreadable_after_stop"

_DETAIL_ROSTER_UNREADABLE = (
    "the daemon roster is unreadable, so no worker can be shown to be live"
)
_DETAIL_ROSTER_UNTRUSTED = (
    "the daemon roster is empty while other sessions are recorded live, which"
    " looks like a daemon restart, so a missing worker proves nothing"
)
_DETAIL_ROSTER_UNCORROBORATED = (
    "the daemon roster changed between two reads, so the worker's absence is"
    " not corroborated"
)
_DETAIL_UNREADABLE_BEFORE_STOP = (
    "the daemon roster became unreadable just before the stop"
)
_DETAIL_NO_LONGER_CANDIDATE = "it is no longer a routed-result session for this ticket"
_PROBLEMS = {
    _REASON_DRAINING: "its background work is still draining",
    _REASON_STILL_LISTED: "its worker is still listed in the roster",
    _REASON_UNREADABLE_AFTER_STOP: "the daemon roster is unreadable after the stop",
}
_NO_REAPPROVE = (
    "Do NOT re-run approve: the row is already PENDING, so a retry never"
    " reaches the close."
)
_SKIPPED_LOG = (
    "routed_orphan_close_skipped: ticket_id=%s client=%s command=%s"
    " session_ids=%s reason=%s"
)


class _OrphanCloseCall(NamedTuple):
    """The fixed context of one :func:`close_routed_result_sessions_for_ticket`.

    ``candidate_ids``/``candidate_surfaces`` are this call's whole marker
    candidate set: the outage checks exclude exactly that set, so one
    candidate's stop can never make another (still ACTIVE) candidate look
    like an unrelated live session.
    """

    ticket_id: str
    client: str
    command: str
    after_transition: bool
    config: OrchestratorConfig
    daemon: NativeDaemonClient
    candidate_ids: frozenset[str]
    candidate_surfaces: frozenset[str]

    @property
    def releasing(self) -> tuple[str, str] | None:
        """The ticket whose parked rows a requeue releases; None for approve."""
        return None if self.after_transition else (self.client, self.ticket_id)


class _Revalidated(NamedTuple):
    """A target that passed the pre-stop re-validation, re-read fresh."""

    session: Session
    outcome: Literal["stop", "flip"]
    live: set[str]


def _read_live_roster(daemon: NativeDaemonClient) -> set[str] | None:
    """The roster's live short ids, or None when it cannot be read."""
    try:
        return daemon.list_live_session_short_ids_fail_closed()
    except (OSError, ValueError):
        return None


def _report_skipped(
    sessions: list[Session],
    call: _OrphanCloseCall,
    *,
    reason: str,
    detail: str = "",
) -> None:
    """Name each skipped session on stderr, then log one WARNING for all."""
    if not sessions:
        return
    for session in sessions:
        if reason == _REASON_DRAINING:
            line = (
                f"Left running: routed-result session {session.id} for"
                f" {call.ticket_id} ({call.client}) was not closed because its"
                " background work is still draining. It completes on its own"
                " when that work ends; if it never does, close it with:"
                f" {close_command(session.id)}"
            )
        else:
            line = (
                f"Warning: routed-result session {session.id} for"
                f" {call.ticket_id} ({call.client}) was NOT closed: {detail}."
                " Once its worker is gone, close it with:"
                f" {close_command(session.id)}"
            )
        click.echo(line, err=True)
    logger.warning(
        _SKIPPED_LOG,
        call.ticket_id,
        call.client,
        call.command,
        ",".join(s.id for s in sessions),
        reason,
    )


def _report_pinned(pinned: list[PinnedOrphan], call: _OrphanCloseCall) -> None:
    """Name each pinned session and its pinning row, then log one WARNING."""
    if not pinned:
        return
    for item in pinned:
        row = item.pinned_by
        click.echo(
            f"Left running: routed-result session {item.session.id} for"
            f" {call.ticket_id} ({call.client}) is pinned by {row.ticket_id}"
            f" ({row.client}, {row.status.name}) and was not closed. If it is"
            f" dead, close it with: {close_command(item.session.id)}",
            err=True,
        )
    logger.warning(
        _SKIPPED_LOG,
        call.ticket_id,
        call.client,
        call.command,
        ",".join(item.session.id for item in pinned),
        _REASON_PINNED,
    )


def _orphan_refusal(
    session: Session, call: _OrphanCloseCall, *, problem: str, remedy: str
) -> CwError:
    """The one place the routed-orphan refusal wording lives."""
    if call.after_transition:
        lead = (
            f"Approved {call.ticket_id} ({call.client}), but routed-result session"
            f" {session.id} could not be closed: {problem}. The row is released but"
            " the worktree stays occupied until it is closed."
        )
    else:
        lead = (
            f"Cannot close routed-result session {session.id} for"
            f" {call.ticket_id} ({call.client}): {problem}. The row was not"
            " requeued."
        )
    return CwError(f"{lead} {remedy}")


def _stop_remedy(session: Session, call: _OrphanCloseCall, surface_ref: str) -> str:
    """What to do after a stop the roster never confirmed."""
    base = (
        f"Run `claude stop {surface_ref}`; if the stop keeps failing,"
        " the roster entry is stale and the roster file"
        f" ({call.daemon.roster_path}) must be repaired."
    )
    if call.after_transition:
        return (
            f"{base} {_NO_REAPPROVE} Once the worker is gone, close the session"
            f" with: {close_command(session.id)}."
        )
    return (
        f"{base} Re-run the requeue after the stop works: the retry finds the"
        " worker gone from the roster and closes the session by a status flip"
        " alone."
    )


def _flip_failed_remedy(session: Session, call: _OrphanCloseCall) -> str:
    """What to do when the worker is gone but the status flip failed."""
    if call.after_transition:
        return f"{_NO_REAPPROVE} Close the session with: {close_command(session.id)}."
    return (
        "Re-run the requeue (the retry closes the session by a status flip"
        f" alone), or run: {close_command(session.id)}."
    )


def _handle_draining(draining: list[Session], call: _OrphanCloseCall) -> None:
    """Leave draining sessions running (approve) or refuse the requeue.

    A requeue would refuse on the still-live session anyway, so it refuses up
    front with the reason and nothing is stopped; after an approve the
    approval stands, and the session completes on its own when its Stop fires.
    """
    if not draining:
        return
    if call.after_transition:
        _report_skipped(draining, call, reason=_REASON_DRAINING)
        return
    for session in draining:
        logger.warning(
            "routed_orphan_close_refused: ticket_id=%s client=%s session_id=%s"
            " command=%s reason=%s",
            call.ticket_id,
            call.client,
            session.id,
            call.command,
            _REASON_DRAINING,
        )
    first = draining[0]
    remedy = (
        "Wait for that work to finish (the session then completes on its own)"
        " and re-run the requeue, or, once it is confirmed dead, close the"
        f" session with: {close_command(first.id)}."
    )
    raise _orphan_refusal(
        first, call, problem=_PROBLEMS[_REASON_DRAINING], remedy=remedy
    )


def _fresh_outcome(
    current: Session, split: RoutedOrphanSplit, call: _OrphanCloseCall
) -> Literal["stop", "flip"] | None:
    """*current*'s outcome in a fresh classification; None after a skip."""
    pinned = [item for item in split.pinned if item.session.id == current.id]
    if pinned:
        _report_pinned(pinned, call)
        return None
    if any(s.id == current.id for s in split.roster_untrusted):
        _report_skipped(
            [current],
            call,
            reason=_REASON_ROSTER_UNREADABLE,
            detail=_DETAIL_ROSTER_UNTRUSTED,
        )
        return None
    if any(s.id == current.id for s in split.draining):
        _handle_draining([current], call)
        return None
    return "stop" if any(s.id == current.id for s in split.stop) else "flip"


def _revalidate_before_stop(
    session: Session, call: _OrphanCloseCall
) -> _Revalidated | None:
    """Re-read state, queue and roster and re-classify just before the act.

    Re-runs the classifier over the FULL fresh candidate set (so the outage
    exclusion matches the initial classification) and looks *session* up in
    it. Lock-free, like the rest of the helper: the window to the act is
    sub-second, and the flip itself re-reads the session under
    ``sessions_lock``.
    """
    state = load_state()
    fresh = routed_marker_candidates(
        state, client=call.client, ticket_id=call.ticket_id
    )
    current = next((c for c in fresh if c.id == session.id), None)
    if current is None:
        _report_skipped(
            [session],
            call,
            reason=_REASON_NO_LONGER_CANDIDATE,
            detail=_DETAIL_NO_LONGER_CANDIDATE,
        )
        return None
    live = _read_live_roster(call.daemon)
    if live is None:
        _report_skipped(
            [current],
            call,
            reason=_REASON_UNREADABLE_BEFORE_STOP,
            detail=_DETAIL_UNREADABLE_BEFORE_STOP,
        )
        return None
    split = split_resolvable_routed_sessions(
        fresh,
        load_dev_queue().tasks,
        state=state,
        native_live=live,
        now=datetime.now(UTC),
        config=call.config,
        releasing=call.releasing,
    )
    outcome = _fresh_outcome(current, split, call)
    return None if outcome is None else _Revalidated(current, outcome, live)


def _absence_corroborated(
    session: Session, call: _OrphanCloseCall, first_read: set[str]
) -> bool:
    """True when a second roster read, one interval later, matches the first.

    A flip-only close infers "no worker" from absence; a roster caught
    mid-rewrite can be readable, non-empty and still partial, so the absence
    must hold across two reads before the worktree is released.
    """
    time.sleep(_ROUTED_STOP_CONFIRM_INTERVAL_SECS)
    if _read_live_roster(call.daemon) == first_read:
        return True
    _report_skipped(
        [session],
        call,
        reason=_REASON_ROSTER_UNREADABLE,
        detail=_DETAIL_ROSTER_UNCORROBORATED,
    )
    return False


def _unconfirmed_reason(
    *,
    gone: bool,
    after: set[str] | None,
    surface_ref: str,
    call: _OrphanCloseCall,
    pre_stop_live: set[str],
) -> str | None:
    """Why the stop is not confirmed, or None when the worker is gone.

    A post-stop roster that reads empty is outage-shaped -- not a
    confirmation -- only when the pre-stop roster also listed workers beyond
    this call's candidates AND state still records a live session outside
    them (N1 + the pre-stop snapshot): the orphan's own stop can empty a
    roster that only ever held the orphan, however stale an unrelated session
    in state is, and that must not refuse forever.
    """
    if after is None:
        return _REASON_UNREADABLE_AFTER_STOP
    if not gone or surface_ref in after:
        return _REASON_STILL_LISTED
    if (
        not after
        and pre_stop_live - call.candidate_surfaces
        and _roster_outage_shaped(load_state(), after, exclude_ids=call.candidate_ids)
    ):
        return _REASON_UNREADABLE_AFTER_STOP
    return None


def _stop_until_gone(
    session: Session, call: _OrphanCloseCall, *, pre_stop_live: set[str]
) -> None:
    """Stop *session*'s worker and confirm it left the roster, else refuse.

    Fail closed (R4): a worker still listed after the wait, or a roster that
    cannot be read (or reads outage-shaped) after the stop, raises the
    refusal; the session is never flipped COMPLETED on an unconfirmed stop.
    """
    # A marker candidate always carries a surface (_is_routed_marker_candidate).
    surface_ref = cast("str", session.surface_ref)
    call.daemon.stop(surface_ref)
    wait_error: OSError | ValueError | None = None
    try:
        gone = wait_for_roster_presence(
            call.daemon,
            surface_ref,
            present=False,
            timeout=_ROUTED_STOP_CONFIRM_TIMEOUT_SECS,
            interval=_ROUTED_STOP_CONFIRM_INTERVAL_SECS,
        )
    except (OSError, ValueError) as exc:
        gone, wait_error = False, exc
    reason = _unconfirmed_reason(
        gone=gone,
        after=_read_live_roster(call.daemon),
        surface_ref=surface_ref,
        call=call,
        pre_stop_live=pre_stop_live,
    )
    if reason is None:
        return
    logger.warning(
        "routed_orphan_stop_unconfirmed: ticket_id=%s client=%s session_id=%s"
        " command=%s reason=%s",
        call.ticket_id,
        call.client,
        session.id,
        call.command,
        reason,
        exc_info=wait_error,
    )
    raise _orphan_refusal(
        session,
        call,
        problem=_PROBLEMS[reason],
        remedy=_stop_remedy(session, call, surface_ref),
    )


def _record_close_audit(
    session: Session,
    call: _OrphanCloseCall,
    *,
    confirmation_result: str,
) -> None:
    """Persist the routed-orphan close audit, or surface its failure."""
    surface_ref = cast("str", session.surface_ref)
    metadata = {
        "actor": "operator",
        "command": call.command,
        "ticket_id": call.ticket_id,
        "client": call.client,
        "session_id": session.id,
        "surface_ref": surface_ref,
        "prior_status": session.status.value,
        "resulting_status": SessionStatus.COMPLETED.value,
        "confirmation_result": confirmation_result,
        "reason": "routed_result_orphan_resolved",
    }
    event = HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client=session.client,
        session_id=session.id,
        session_name=session.name,
        purpose=session.purpose,
        detail="routed_result_orphan_resolved",
        metadata=metadata,
    )
    try:
        record_event(session.client, event)
    except (OSError, ValueError) as exc:
        logger.exception(
            "routed_orphan_close_audit_failed: ticket_id=%s client=%s"
            " session_id=%s command=%s",
            call.ticket_id,
            call.client,
            session.id,
            call.command,
        )
        msg = (
            f"Routed-result session {session.id} was closed, but its durable"
            " audit record could not be written: "
            f"{exc}"
        )
        raise CwError(msg) from exc


def _queue_close_audit_intent(session: Session, call: _OrphanCloseCall) -> None:
    """Write the shared durable close intent before mutating session state."""
    surface_ref = cast("str", session.surface_ref)
    hit = StrandedRoutedSession(
        session=session,
        ticket_id=call.ticket_id,
        lane=DEFAULT_LANE,
        stage=DEFAULT_STAGE,
        row_status=None,
        stale_minutes=0.0,
        surface_ref=surface_ref,
    )
    try:
        _queue_audit_intents([hit])
    except (OSError, ValueError) as exc:
        logger.exception(
            "routed_orphan_close_audit_intent_failed: ticket_id=%s client=%s"
            " session_id=%s command=%s",
            call.ticket_id,
            call.client,
            session.id,
            call.command,
        )
        message = (
            f"Could not prepare the durable audit record for routed-result session"
            f" {session.id} before closing it: {exc}. Fix the audit outbox and"
            " retry the command."
        )
        raise CwError(message) from exc


def _finalize_close_audit_intent(session: Session, *, stop_succeeded: bool) -> None:
    """Finalize and deliver the shared audit after the status mutation."""
    record = _finalize_audit_intent(
        session.id,
        mutations=(
            ["session_status_completed", "daemon_stopped"]
            if stop_succeeded
            else ["session_status_completed"]
        ),
        stop_succeeded=stop_succeeded,
        stop_error=None,
    )
    if record is not None:
        _emit_audit_record(record)


def _flip_closed(
    session: Session, call: _OrphanCloseCall, *, confirmation_result: str
) -> None:
    """Flip the confirmed-gone (or proven-absent) session COMPLETED."""
    _queue_close_audit_intent(session, call)
    try:
        _spawn_close_impl(
            session_id=session.id,
            native_daemon=call.daemon,
            surface_already_stopped=True,
        )
    except CwError as exc:
        logger.warning(
            "routed_orphan_close_failed: ticket_id=%s client=%s session_id=%s"
            " command=%s error=%s",
            call.ticket_id,
            call.client,
            session.id,
            call.command,
            exc,
            exc_info=True,
        )
        problem = (
            f"the worker is gone but the session status could not be flipped ({exc})"
        )
        raise _orphan_refusal(
            session, call, problem=problem, remedy=_flip_failed_remedy(session, call)
        ) from exc
    try:
        _record_close_audit(
            session,
            call,
            confirmation_result=confirmation_result,
        )
    finally:
        _finalize_close_audit_intent(
            session,
            stop_succeeded=confirmation_result == "worker_gone_from_roster",
        )
    # WARNING, not INFO (N2; the reasoning native_daemon.py gives for its
    # spawn-time usage-limit line): cw.cli._base._configure_logging uses
    # basicConfig at WARNING unless -v is passed, and this line is the only
    # audit record of a daemon stop plus a status flip.
    logger.warning(
        "routed_orphan_closed_on_resolve: ticket_id=%s client=%s session_id=%s"
        " command=%s",
        call.ticket_id,
        call.client,
        session.id,
        call.command,
    )
    click.echo(
        f"Closed orphaned routed-result session {session.id} for"
        f" {call.ticket_id} ({call.client})."
    )


def _close_one(session: Session, call: _OrphanCloseCall) -> bool:
    """Re-validate, stop-and-confirm or corroborate, then flip; True if closed."""
    checked = _revalidate_before_stop(session, call)
    if checked is None:
        return False
    if checked.outcome == "stop":
        _stop_until_gone(checked.session, call, pre_stop_live=checked.live)
        confirmation_result = "worker_gone_from_roster"
    elif not _absence_corroborated(checked.session, call, checked.live):
        return False
    else:
        confirmation_result = "absent_from_readable_roster"
    _flip_closed(
        checked.session,
        call,
        confirmation_result=confirmation_result,
    )
    return True


def close_routed_result_sessions_for_ticket(
    ticket_id: str,
    client: str,
    *,
    command: str,
    config: OrchestratorConfig,
    after_transition: bool,
    native_daemon: NativeDaemonClient | None = None,
    precheck: Callable[[frozenset[str]], None] | None = None,
) -> list[str]:
    """Close (*ticket_id*, *client*)'s orphaned routed-result sessions (#2517).

    *after_transition* is True for approve (the row is already released) and
    False for requeue (the close precedes ``requeue_ticket``, so that ticket's
    own parked rows do not pin, and a draining session refuses). *precheck* is
    called with the ids about to be closed, before anything is stopped, so a
    predicted ``requeue_ticket`` refusal stops nothing. *command* names the
    caller in the audit lines.

    With no marker candidate it returns ``[]`` before resolving a daemon
    client or reading the roster or queue. Returns the closed ids, each also
    printed on stdout at close time, so they are reported even if the
    caller's next step fails. Raises :class:`CwError` on a refusal; sessions
    closed earlier in the same call stay closed.
    """
    state = load_state()
    candidates = routed_marker_candidates(state, client=client, ticket_id=ticket_id)
    if not candidates:
        return []
    daemon = native_daemon or get_native_daemon_client()
    call = _OrphanCloseCall(
        ticket_id=ticket_id,
        client=client,
        command=command,
        after_transition=after_transition,
        config=config,
        daemon=daemon,
        candidate_ids=frozenset(c.id for c in candidates),
        candidate_surfaces=frozenset(cast("str", c.surface_ref) for c in candidates),
    )
    live = _read_live_roster(daemon)
    if live is None:
        _report_skipped(
            candidates,
            call,
            reason=_REASON_ROSTER_UNREADABLE,
            detail=_DETAIL_ROSTER_UNREADABLE,
        )
        return []
    split = split_resolvable_routed_sessions(
        candidates,
        load_dev_queue().tasks,
        state=state,
        native_live=live,
        now=datetime.now(UTC),
        config=config,
        releasing=call.releasing,
    )
    _report_pinned(split.pinned, call)
    _report_skipped(
        split.roster_untrusted,
        call,
        reason=_REASON_ROSTER_UNREADABLE,
        detail=_DETAIL_ROSTER_UNTRUSTED,
    )
    _handle_draining(split.draining, call)
    targets = [*split.stop, *split.flip_only]
    if not targets:
        return []
    if precheck is not None:
        precheck(frozenset(s.id for s in targets))
    return [s.id for s in targets if _close_one(s, call)]
