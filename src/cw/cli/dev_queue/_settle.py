"""Make ``approve``/``requeue`` see the row's real state (#2692).

A worker can record its terminal sentinel (``session.last_result``) before
its SESSION_COMPLETED event is routed, so the dev-queue row still reads
RUNNING and the verb would refuse a row ``cw dev-queue wait`` already reports
as parked. :func:`settle_lagging_row` closes that gap through the canonical
routing seam -- the same ``cw.dispatch.loop`` read/apply halves
``consume_completed_sessions`` composes -- never by acting on the sentinel
directly, so every dispatch gate (scope tier, review health, staleness,
signoff, finalize hold) still applies.

Lock rules (ADR-0019): settle holds no lock itself; the one lock is the
STATE-rank ``dev_queue_lock(bounded=True)`` in :func:`_consume_pending_bounded`,
preceded only by a lock-free event read, never nested under
``sessions_lock``, and its :class:`~cw.exceptions.LockTimeoutError` is caught
so the verb proceeds. Settlement consumes the pending completion prefix through
this row's event, so unrelated earlier events are routed in cursor order too.

Also holds :func:`live_session_notes`, the read-only diagnostic the
idempotent PENDING ``requeue`` no-op prints instead of the live-session guard
it skips.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, NamedTuple

import click

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES
from cw.config import load_state
from cw.dev_queue import _find_ticket, dev_queue_lock, load_dev_queue
from cw.dev_queue.next_commands import dev_queue_command
from cw.dispatch.loop import apply_pending_completions, read_pending_completions
from cw.exceptions import CwError, LockTimeoutError
from cw.models import QueueItemStatus
from cw.reconcile._shared import (
    find_live_sessions_for_ticket,
    resolve_session_for_task,
)

if TYPE_CHECKING:
    from cw.models import Session, TicketTask
    from cw.native_daemon import NativeDaemonClient

_SETTLE_TIMEOUT_SECONDS = 10.0
_SETTLE_POLL_SECONDS = 1.0
_READ_ERRORS = (OSError, ValueError, CwError)

_LIVE_SESSION_NOTE = (
    "note: session {sid} ({name}) for this ticket is live in the daemon roster;"
    " this no-op stopped nothing. To close it, run:"
    " `cw spawn close {sid} --requeue`."
)
_ROSTER_UNREADABLE_NOTE = (
    "note: the daemon roster could not be read, so a live session for this"
    " ticket cannot be ruled out; this no-op stopped nothing. If one is known,"
    " close it with `cw spawn close <sid> --requeue`."
)


class SettleOutcome(NamedTuple):
    """What :func:`settle_lagging_row` saw.

    ``row`` is the (possibly refreshed) row, or None when it is missing or
    unreadable (the verb then raises its own error). ``routed`` is True iff
    the row was RUNNING at the first read and is no longer RUNNING on return.
    """

    row: TicketTask | None
    routed: bool


def _read_row(ticket_id: str, client: str) -> TicketTask | None:
    try:
        return _find_ticket(load_dev_queue(), ticket_id, client, warn=False)
    except _READ_ERRORS:
        return None


def _recorded_terminal_status(session: Session | None) -> str | None:
    """The session's recorded sentinel status when it is a park/terminal one.

    ``stage_complete`` (an intermediate advance dispatch moves on by itself)
    is excluded, exactly as ``cw dev-queue wait`` excludes it.
    """
    if session is None or not isinstance(session.last_result, dict):
        return None
    status = session.last_result.get("status")
    if not isinstance(status, str) or not status:
        return None
    if status in INTERMEDIATE_ADVANCE_STATUSES:
        return None
    return status


def _consume_pending_bounded(session_id: str) -> bool:
    """Route pending completions if one is *session_id*'s; True iff it was."""
    events = read_pending_completions()
    target_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.payload.get("session_id") == session_id
        ),
        None,
    )
    if target_index is None:
        return False
    with dev_queue_lock(bounded=True):
        # The dispatch cursor is ordered. Apply the complete prefix through
        # this row's event so earlier unrelated events are not skipped.
        apply_pending_completions(events[: target_index + 1])
    return True


def _note(text: str) -> None:
    click.echo(text, err=True)


def settle_lagging_row(ticket_id: str, client: str) -> SettleOutcome:
    """Route a RUNNING row whose session already recorded its result.

    Polls up to ``_SETTLE_TIMEOUT_SECONDS`` (first attempt immediate) while the
    row's completion event is not yet pending; stops at once when routing ran
    but left the row RUNNING (a stage-mismatch refusal). Never raises: every
    failure falls through with a stderr note so the verb refuses as before.
    """
    row = _read_row(ticket_id, client)
    if row is None or row.status != QueueItemStatus.RUNNING:
        return SettleOutcome(row, routed=False)
    try:
        session = resolve_session_for_task(row, load_state())
    except _READ_ERRORS:
        return SettleOutcome(row, routed=False)
    status = _recorded_terminal_status(session)
    if session is None or status is None:
        return SettleOutcome(row, routed=False)
    return _settle_loop(row, session.id, status)


def _settle_loop(row: TicketTask, session_id: str, status: str) -> SettleOutcome:
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    while True:
        try:
            consumed = _consume_pending_bounded(session_id)
        except LockTimeoutError as exc:
            _note(
                "could not take the dev-queue lock to route session"
                f" {session_id}'s recorded {status} ({exc}); continuing with the"
                " row as it stands"
            )
            refreshed = _read_row(row.ticket_id, row.client)
            routed = (
                refreshed is not None and refreshed.status != QueueItemStatus.RUNNING
            )
            return SettleOutcome(refreshed, routed=routed)
        except _READ_ERRORS as exc:
            _note(
                f"could not read or route the recorded completion for session"
                f" {session_id} ({exc}); continuing with the row as it stands"
            )
            return SettleOutcome(row, routed=False)
        refreshed = _read_row(row.ticket_id, row.client)
        if refreshed is None:
            return SettleOutcome(None, routed=False)
        if refreshed.status != QueueItemStatus.RUNNING:
            _note(
                f"row {row.ticket_id} was RUNNING; session {session_id} had recorded"
                f" {status}; routed -> {refreshed.status.value}"
            )
            return SettleOutcome(refreshed, routed=True)
        if consumed:
            _note(
                f"session {session_id} recorded {status} but routing left the row"
                " RUNNING (stage mismatch?); see:"
                f" {dev_queue_command('show', row)}"
            )
            return SettleOutcome(refreshed, routed=False)
        if time.monotonic() >= deadline:
            _note(
                f"session {session_id} recorded {status} but its completion was not"
                f" routed within {_SETTLE_TIMEOUT_SECONDS:g}s (the worker may not"
                " have finished; the Stop hook emits completion); try:"
                f" {dev_queue_command('wait', row)}"
            )
            return SettleOutcome(refreshed, routed=False)
        time.sleep(_SETTLE_POLL_SECONDS)


def live_session_notes(
    ticket_id: str, client: str, daemon: NativeDaemonClient
) -> list[str]:
    """Read-only notes naming any daemon-live session for the ticket.

    The same fail-closed roster read and liveness helper the requeue
    live-session guard uses, so the two cannot disagree; nothing is stopped
    or written. Any read error yields no note.
    """
    try:
        live_ids = daemon.list_live_session_short_ids_fail_closed()
        if live_ids is None:
            return [_ROSTER_UNREADABLE_NOTE]
        live = find_live_sessions_for_ticket(load_state(), ticket_id, client, live_ids)
    except _READ_ERRORS:
        return []
    return [_LIVE_SESSION_NOTE.format(sid=s.id, name=s.name) for s in live]
