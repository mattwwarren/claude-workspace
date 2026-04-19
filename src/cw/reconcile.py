"""Reconcile cw session state with live multiplexer surfaces.

The authoritative view of what is running lives in the multiplexer
(tmux/cmux/fake). :func:`compute_drift` compares ``sessions.json`` against
that view and returns a :class:`ReconcileReport` naming the sessions that
have gone phantom — active/idle rows whose ``surface_ref`` no longer maps
to any live surface. :func:`reconcile` (Task 5) applies the report under
the state lock.

The split is deliberate: ``compute_drift`` is pure and testable in
isolation; ``reconcile`` does the side-effecting work (state mutation,
event emission, dev-queue revert).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)

if TYPE_CHECKING:
    from cw.cmux import MultiplexerAdapter
    from cw.models import CwState


# Only these two statuses imply "the multiplexer should have a surface".
# BACKGROUNDED sessions intentionally have no pane (that's the whole point);
# COMPLETED is terminal. Both are ignored by reconciliation.
_LIVE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
    }
)


@dataclass(frozen=True)
class ReconcileReport:
    """What reconciliation would do / did.

    ``phantom_session_ids`` — sessions whose ``surface_ref`` is not in the
    live set. Ordered by the original order in ``state.sessions``.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`, empty after
    :func:`compute_drift`.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)


def compute_drift(state: CwState, adapter: MultiplexerAdapter) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in ``adapter.list_surfaces()``.

    This function does not mutate state.
    """
    live = adapter.list_surfaces()
    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref not in live:
            phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)


# Daemon session names follow "client/auto-dev/<ticket-id>" (see
# src/cw/dispatch.py::dispatch_tick where the label is constructed).
_AUTO_DEV_LABEL_PREFIX = "auto-dev/"


def _ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(_AUTO_DEV_LABEL_PREFIX):
        return tail[len(_AUTO_DEV_LABEL_PREFIX) :]
    return None


def reconcile(adapter: MultiplexerAdapter) -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Partial-failure note: state and the dev queue are separate files. If
    ``save_state`` succeeds but the subsequent dev-queue update raises,
    the session will be COMPLETED while its TicketTask stays RUNNING.
    The next ``reconcile()`` call will not pick this up because the
    session is no longer ACTIVE/IDLE — so a stranded RUNNING task can
    only be recovered by explicit operator action. This is an acceptable
    tradeoff for a file-based, single-user tool.
    """
    state = load_state()
    drift = compute_drift(state, adapter)
    if not drift.phantom_session_ids:
        return drift

    phantom_set = set(drift.phantom_session_ids)
    now = datetime.now(UTC)

    ticket_ids_to_revert: list[str] = []
    pending_events: list[dict[str, object]] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        if session.origin is SessionOrigin.DAEMON:
            ticket_id = _ticket_id_for_session(session.name)
            if ticket_id:
                ticket_ids_to_revert.append(ticket_id)
        pending_events.append(
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "crashed": True,
            }
        )

    save_state(state)
    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    reverted: list[str] = []
    if ticket_ids_to_revert:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id in ticket_ids_to_revert
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.PENDING
                    reverted.append(task.ticket_id)
            if reverted:
                save_dev_queue(store)

    return ReconcileReport(
        phantom_session_ids=drift.phantom_session_ids,
        reverted_ticket_ids=reverted,
    )
