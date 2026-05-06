"""Reconcile cw session state with live multiplexer surfaces.

The authoritative view of what is running lives in the multiplexer
(tmux/cmux/fake). :func:`compute_drift` compares ``sessions.json`` against
that view and returns a :class:`ReconcileReport` naming the sessions that
have gone phantom — active/idle rows whose ``surface_ref`` no longer maps
to any live surface. :func:`reconcile` applies the report.

The split is deliberate: ``compute_drift`` is pure and testable in
isolation; ``reconcile`` does the side-effecting work (state mutation,
event emission, dev-queue revert).

Transient-outage safety: ``reconcile`` refuses to mutate state when the
adapter reports *zero* live surfaces but the persisted state still
contains ACTIVE/IDLE sessions with surface refs. A temporary multiplexer
hiccup would otherwise irreversibly mark every session as CRASHED.
``compute_drift`` stays pure and does not apply this guard — callers that
want drift-without-side-effects still get the full phantom list.

Race note: ``reconcile`` does ``load_state → mutate → save_state`` without
a dedicated ``sessions.json`` file lock. This matches every other
``save_state`` call site in the codebase (``cw.session``, ``cw.cli``, …);
a unified state lock is a larger refactor tracked separately. In
practice the race window is the in-memory mutation between load and save,
and concurrent writers are rare in the single-user model this tool
targets.
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


# Session-name prefix for DAEMON sessions spawned by the dispatch loop. The
# full name is ``<client>/<AUTO_DEV_LABEL_PREFIX><ticket_id>``; reconciliation
# uses it to recover the ticket id when reverting phantom tickets. Defined
# here (not in ``cw.dispatch``) to avoid a circular import — ``cw.dispatch``
# imports :func:`reconcile` from this module.
AUTO_DEV_LABEL_PREFIX = "auto-dev/"


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
    ``phantom_session_names`` — session names in the same order as
    ``phantom_session_ids``. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)


def compute_drift(state: CwState, adapter: MultiplexerAdapter) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in ``adapter.list_surfaces()``.

    This function does not mutate state. It also does not distinguish
    "backend reports zero live surfaces" from "backend is unreachable";
    that guard lives in :func:`reconcile`.
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


def ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(AUTO_DEV_LABEL_PREFIX):
        return tail[len(AUTO_DEV_LABEL_PREFIX) :]
    return None


def _looks_like_backend_outage(state: CwState, live: set[str]) -> bool:
    """True when the empty ``live`` set is almost certainly a backend outage.

    If the adapter returned an empty set *and* the persisted state has at
    least one ACTIVE/IDLE session that was once given a surface_ref, we
    assume the multiplexer is unreachable rather than "somehow every
    session died at once". Aborting here is the difference between a
    5-second cmux restart and permanent data loss.
    """
    if live:
        return False
    return any(
        s.surface_ref is not None and s.status in _LIVE_STATUSES for s in state.sessions
    )


def reconcile(adapter: MultiplexerAdapter) -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_backend_outage` matches — a transient multiplexer
    restart must not trigger mass-reaping.

    Partial-failure note: state and the dev queue are separate files. If
    ``save_state`` succeeds but the subsequent dev-queue update raises,
    the session will be COMPLETED while its TicketTask stays RUNNING.
    The next ``reconcile()`` call will not pick this up because the
    session is no longer ACTIVE/IDLE — so a stranded RUNNING task can
    only be recovered by explicit operator action. This is an acceptable
    tradeoff for a file-based, single-user tool.
    """
    state = load_state()
    live = adapter.list_surfaces()
    if _looks_like_backend_outage(state, live):
        return ReconcileReport()

    drift = compute_drift(state, adapter)
    if not drift.phantom_session_ids:
        return drift

    phantom_set = set(drift.phantom_session_ids)
    now = datetime.now(UTC)

    ticket_ids_to_revert: list[str] = []
    pending_events: list[dict[str, object]] = []
    phantom_names: list[str] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        phantom_names.append(session.name)
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id and session.origin is SessionOrigin.DAEMON:
            ticket_ids_to_revert.append(ticket_id)
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": True,
        }
        if ticket_id:
            payload["ticket_id"] = ticket_id
        pending_events.append(payload)

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
        phantom_session_names=phantom_names,
        reverted_ticket_ids=reverted,
    )
