"""Apply SESSION_COMPLETED events to a loaded dev queue (#2999).

Extracted from ``cw.dispatch.loop``, which re-imports
``_apply_events_to_store``. Only the two functions that consult no name the
suite patches on ``cw.dispatch.loop`` moved here; the lock-taking consumer
and the ``load_dev_queue`` / ``load_effective_clients`` callers stay in
``loop`` so those patches keep reaching them. Logs as ``cw.dispatch``, the
pre-split logger name. Never imports ``cw.dispatch.loop`` or the
``cw.dispatch`` package ``__init__``.

The event-to-row lookup (:func:`_running_row_for_event`) and the session
sentinel read (:func:`_session_last_result`) are shared with the pre-lock
REVIEW tip sweep (``review_tip_sweep``, #2994), so both see the same row for
the same event.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from cw.config import load_state
from cw.dev_queue import save_dev_queue
from cw.dispatch.routing import _accumulate_task_cost, apply_staged_decision
from cw.models import QueueItemStatus
from cw.queue_rows import _find_running_row
from cw.reconcile import _USAGE_LIMITED_MID_TURN_REASON, ticket_id_for_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorEvent,
        TicketTask,
    )

_log = logging.getLogger("cw.dispatch")


class _EventRow(NamedTuple):
    """The RUNNING row a completion event names, and the session that owns it."""

    task: TicketTask
    session_id: str


def _reconcile_owns_completion(payload: Mapping[str, object]) -> bool:
    """Is this SESSION_COMPLETED one whose row reconcile dispositions itself?

    Crashed events are emitted by reconcile only. For DAEMON sessions
    reconcile has already reverted the task RUNNING → PENDING; marking the
    task COMPLETED here would shadow that revert and (worse) match the next
    freshly-respawned RUNNING task for the same ticket_id, falsely retiring a
    still-running session. For non-DAEMON crashed sessions reconcile does not
    touch the queue, so a blanket skip is conservative-safe (no queue task is
    expected to match anyway). See GitHub issue #97.

    The mid-turn usage-limit act records its (non-crash) completion before
    it stops the session, then transitions the row itself, resuming from the
    row's ``usage_limit_act`` intent on a later tick if interrupted (#2324).
    Routing that event here would park the row with no sentinel first.
    """
    return bool(payload.get("crashed")) or (
        payload.get("reason") == _USAGE_LIMITED_MID_TURN_REASON
    )


def _event_ticket_id(event: OrchestratorEvent) -> str | None:
    """The event's ``ticket_id``, recovered from ``session_name`` when absent.

    The fallback drains historical RUNNING tasks whose completion events
    predate the producer-side fix.
    """
    ticket_id = event.payload.get("ticket_id")
    if not ticket_id:
        session_name = event.payload.get("session_name")
        if isinstance(session_name, str):
            ticket_id = ticket_id_for_session(session_name)
    return ticket_id if isinstance(ticket_id, str) and ticket_id else None


def _event_session_id(ticket_id: str, event: OrchestratorEvent) -> str | None:
    """The event's ``session_id`` if it can identify a row; ``None`` otherwise.

    A *present but malformed* ``session_id`` and a legacy event with none are
    both skipped and logged, never matched on a bare ``(ticket_id, RUNNING)``
    (#2219).
    """
    event_session_id = event.payload.get("session_id")
    if event_session_id is None:
        _log.debug(
            "legacy_session_completed_event_skipped: ticket=%s "
            "reason=no session_id in payload -- cannot disambiguate a "
            "duplicate RUNNING row, so this event is dropped rather "
            "than applied via a bare (ticket_id, RUNNING) match (#2219)",
            ticket_id,
        )
        return None
    if not isinstance(event_session_id, str) or not event_session_id:
        _log.debug(
            "malformed_session_completed_event_skipped: ticket=%s "
            "event_session_type=%s reason=session_id must be a "
            "non-empty str or None",
            ticket_id,
            type(event_session_id).__name__,
        )
        return None
    return event_session_id


def _running_row_for_event(
    store: DevQueueStore, event: OrchestratorEvent
) -> _EventRow | None:
    """The RUNNING row *event* completes, or ``None`` if it completes none.

    Pure (no I/O beyond the debug log): the caller holds the lock for the
    apply path, and the pre-lock sweep calls it on a lock-free snapshot.
    """
    if _reconcile_owns_completion(event.payload):
        return None
    ticket_id = _event_ticket_id(event)
    if ticket_id is None:
        return None
    event_session_id = _event_session_id(ticket_id, event)
    if event_session_id is None:
        return None
    # The payload doesn't reliably carry the owning client (several
    # producers omit it), so the candidate clients are read off the
    # RUNNING rows for this ticket_id and each is tried in turn --
    # event_session_id is globally unique (TicketTask.session_id ==
    # Session.id), so at most one candidate can ever match.
    candidate_clients = {
        stored_task.client
        for stored_task in store.tasks
        if stored_task.ticket_id == ticket_id
        and stored_task.status == QueueItemStatus.RUNNING
    }
    for client_name in candidate_clients:
        task = _find_running_row(
            store, ticket_id, client_name, session_id=event_session_id
        )
        if task is not None:
            return _EventRow(task, event_session_id)
    return None


def _session_last_result(state: CwState, session_id: str) -> dict[str, Any] | None:
    """The owning session's sentinel dict, or ``None`` when it has none."""
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None or not isinstance(session.last_result, dict):
        return None
    return session.last_result


def _apply_events_to_store(
    store: DevQueueStore,
    events: list[OrchestratorEvent],
    clients: dict[str, ClientConfig],
) -> int:
    """Apply SESSION_COMPLETED events to an already-loaded DevQueueStore.

    Caller must hold ``dev_queue_lock``. Saves the store when tasks were
    transitioned; does NOT advance the event cursor — cursor advancement
    is the caller's responsibility after the lock is released.

    Every match routes through :func:`cw.dispatch.claim._find_running_row`
    keyed on the event's own ``session_id`` (GitHub #97: a stale event from
    an old session must not complete a freshly-respawned task; #1692: a task
    not yet stamped with a session_id cannot own a session-tagged
    completion). A genuinely legacy event -- ``session_id`` absent from the
    payload, or present with value ``None`` -- cannot supply that identity,
    so it is skipped and logged rather than falling back to a bare
    ``(ticket_id, RUNNING)`` match: that fallback could complete the wrong
    duplicate RUNNING row for a ``ticket_id`` (#2219 -- the same hazard
    class this ticket's helper already closes for every other consumer in
    ``src/cw/``). A *present but malformed* ``session_id`` (anything
    non-``None`` that isn't a non-empty ``str``) is likewise skipped and
    logged, never treated as legacy.

    Returns the number of tasks transitioned to COMPLETED.
    """
    completed = 0
    for event in events:
        row = _running_row_for_event(store, event)
        if row is None:
            continue
        state = load_state()
        last_result = _session_last_result(state, row.session_id)
        status = last_result.get("status") if last_result is not None else None

        # #1019: a stage-mismatch refusal is a true no-op (Pre-flight
        # Resolution #4) -- skip cost accumulation and the completed
        # count so a refused/stale sentinel doesn't mutate task state
        # or trigger save_dev_queue below.
        if apply_staged_decision(row.task, status, last_result, clients):
            _accumulate_task_cost(row.task, row.session_id)
            completed += 1
    if completed:
        save_dev_queue(store)
    return completed
