"""Apply SESSION_COMPLETED events to a loaded dev queue (#2999).

Extracted from ``cw.dispatch.loop``, which re-imports
``_apply_events_to_store``. Only the two functions that consult no name the
suite patches on ``cw.dispatch.loop`` moved here; the lock-taking consumer
and the ``load_dev_queue`` / ``load_effective_clients`` callers stay in
``loop`` so those patches keep reaching them. Logs as ``cw.dispatch``, the
pre-split logger name. Never imports ``cw.dispatch.loop`` or the
``cw.dispatch`` package ``__init__``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import save_dev_queue
from cw.dispatch.routing import _accumulate_task_cost, apply_staged_decision
from cw.models import QueueItemStatus
from cw.queue_rows import _find_running_row
from cw.reconcile import _USAGE_LIMITED_MID_TURN_REASON, ticket_id_for_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.models import ClientConfig, DevQueueStore, OrchestratorEvent

_log = logging.getLogger("cw.dispatch")


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
        if _reconcile_owns_completion(event.payload):
            continue
        ticket_id = event.payload.get("ticket_id")
        if not ticket_id:
            # Fallback: recover ticket_id from the session_name for events
            # produced before the reconciler emitted ticket_id explicitly.
            # Drains historical RUNNING tasks whose completion events
            # predate the producer-side fix.
            session_name = event.payload.get("session_name")
            if isinstance(session_name, str):
                ticket_id = ticket_id_for_session(session_name)
        if not ticket_id:
            continue
        event_session_id = event.payload.get("session_id")
        if event_session_id is not None and (
            not isinstance(event_session_id, str) or not event_session_id
        ):
            _log.debug(
                "malformed_session_completed_event_skipped: ticket=%s "
                "event_session_type=%s reason=session_id must be a "
                "non-empty str or None",
                ticket_id,
                type(event_session_id).__name__,
            )
            continue
        if event_session_id is None:
            _log.debug(
                "legacy_session_completed_event_skipped: ticket=%s "
                "reason=no session_id in payload -- cannot disambiguate a "
                "duplicate RUNNING row, so this event is dropped rather "
                "than applied via a bare (ticket_id, RUNNING) match (#2219)",
                ticket_id,
            )
            continue
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
        task = None
        for client_name in candidate_clients:
            task = _find_running_row(
                store, ticket_id, client_name, session_id=event_session_id
            )
            if task is not None:
                break
        if task is None:
            continue
        state = load_state()
        session = next(
            (s for s in state.sessions if s.id == event_session_id),
            None,
        )
        last_result = (
            session.last_result
            if session is not None and isinstance(session.last_result, dict)
            else None
        )
        status = last_result.get("status") if last_result is not None else None

        # #1019: a stage-mismatch refusal is a true no-op (Pre-flight
        # Resolution #4) -- skip cost accumulation and the completed
        # count so a refused/stale sentinel doesn't mutate task state
        # or trigger save_dev_queue below.
        if apply_staged_decision(task, status, last_result, clients):
            _accumulate_task_cost(task, event_session_id)
            completed += 1
    if completed:
        save_dev_queue(store)
    return completed
