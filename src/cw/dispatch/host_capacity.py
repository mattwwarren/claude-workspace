"""Fleet-wide host-capacity admission gate for the dispatch loop.

Part of the ``cw.dispatch`` package (#1444): a single optional ceiling on how
many DAEMON sessions may run concurrently across the whole host, independent
of (and folded into) the existing per-client ceiling in ``lanes.py``.
Structurally mirrors ``cw.dispatch.gating``'s "fleet-wide value resolved once
per tick" shape, minus its TTL-cache/latch machinery -- this is a pure
in-memory computation over state and queue snapshots the caller already
loaded once this tick (no subprocess, no sidecar file), so there is nothing
worth memoizing across ticks.

Amended R4 (ghost-lockout fix): a naive ``Session.status in (ACTIVE, IDLE)``
count was disproven during planning. Under ``ReapPolicy.SIGNAL_ONLY`` (the
default), a stalled session's owning ``TicketTask`` is routed to
``BLOCKED_ON_USER`` / ``AWAITING_OPERATOR_SIGNOFF`` for operator inspection,
but the ``Session`` row itself is left untouched -- daemon not stopped,
``session_id`` not cleared (see ``cw.reconcile.phantom._route_phantom_by_policy``
and ``cw.reconcile._shared._apply_queue_mutations``'s ``clear_session_id``
handling). Left uncorrected, that "ghost" session would count toward
``host_running`` forever, permanently consuming one unit of host budget until
an operator manually intervenes -- silently strangling throughput on every
future tick until someone notices. The join below excludes a session only
when its owning task is CONFIRMED parked (BLOCKED_ON_USER or
AWAITING_OPERATOR_SIGNOFF, joined via ``TicketTask.session_id ==
Session.id``); a session with no owning task at all, or whose task is
RUNNING, still counts normally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import QueueItemStatus, SessionOrigin, SessionStatus

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore, OrchestratorConfig

# The two QueueItemStatus values under which a task's owning session is
# parked for operator inspection (ADR-0006 / RFC 0007 Phase 3, #990) rather
# than actively working -- see the module docstring for why a session parked
# under either must not count toward host_running.
_PARKED_STATUSES = frozenset(
    {QueueItemStatus.BLOCKED_ON_USER, QueueItemStatus.AWAITING_OPERATOR_SIGNOFF}
)


def resolve_host_capacity(
    state: CwState, queue: DevQueueStore, config: OrchestratorConfig
) -> tuple[int, int | None]:
    """Resolve the fleet-wide running DAEMON count and host budget for this tick.

    Returns ``(host_running, host_budget)``:

    - ``host_budget`` is ``config.host_session_budget`` passed through
      unchanged (``None`` means the feature is off; the caller folds it into
      per-client admission only when not ``None``).
    - ``host_running`` counts DAEMON-origin sessions in ACTIVE or IDLE status
      across every client, EXCLUDING any session whose owning ``TicketTask``
      is parked in BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF (see the
      module docstring's amended-R4 rationale).

    Client-filter-independent by construction (no client parameter) and
    intended to be computed exactly once per tick by the caller
    (``dispatch_tick``), against the already-loaded, unfiltered fleet-wide
    ``state``/``queue`` snapshots -- this function performs no I/O of its
    own.
    """
    parked_session_ids = {
        task.session_id
        for task in queue.tasks
        if task.session_id is not None and task.status in _PARKED_STATUSES
    }
    host_running = sum(
        1
        for session in state.sessions
        if session.origin == SessionOrigin.DAEMON
        and session.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        and session.id not in parked_session_ids
    )
    return host_running, config.host_session_budget
