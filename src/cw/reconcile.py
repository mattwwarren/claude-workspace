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
from typing import TYPE_CHECKING

from cw.models import SessionStatus

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
