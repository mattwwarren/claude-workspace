"""Dispatch-claim event emitters.

Per-task ``dispatch.tick`` and ``SESSION_NEEDS_ATTENTION`` payload builders
shared by the claim screen (:mod:`cw.dispatch.claim.screening`) and the spawn
path's occupancy deferrals (:mod:`cw.dispatch.claim.spawn`). Extracted verbatim
from the historical flat ``cw.dispatch.claim`` module by the package split
(#2378).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import STALE_DISPATCH_GATE_DISPOSITION
from cw.events import record_event
from cw.models import DispatchSkipReason, OrchestratorEventType

if TYPE_CHECKING:
    from cw.models import TicketTask


def _emit_attempt_cap_blocked_event(
    client_name: str, ticket_id: str, ceiling: int
) -> None:
    """Emit a dispatch.tick event when a task is parked at the attempt ceiling.

    Per-task event (not per-client-per-tick) for operator observability: a quiet
    loop after several failures should be distinguishable from a healthy idle loop.
    See GitHub #786.

    ``attempt_ceiling`` carries the *resolved* number that actually fired
    (#1751). Since the ceiling became lane-scoped, an operator reading this
    event can no longer infer it from ``global_attempt_ceiling`` — the lane may
    have overridden it. Additive: this payload is a plain untyped dict, so no
    consumer schema migration is involved.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
            "ticket_id": ticket_id,
            "attempt_ceiling": ceiling,
        },
    )


def _emit_attempt_cap_attention_event(
    task: TicketTask, client_name: str, lane: str, ceiling: int
) -> None:
    """Emit SESSION_NEEDS_ATTENTION when a task is parked at the attempt ceiling.

    Sibling of :func:`_emit_attempt_cap_blocked_event` (#1257) -- that helper
    emits a DISPATCH_TICK event (operator-visible tick summary); this one
    emits the SESSION_NEEDS_ATTENTION event the attention-monitor/board
    surfaces consume, using the same canonical 9-field payload shape as
    ``_route_scope_gated_approval`` in routing.py. No session/breadcrumb
    detail exists at this pre-spawn ceiling check (the task never spawned a
    session this attempt), so ``session_id``/``session_name``/``breadcrumbs``
    are empty/None.

    ``attempt_ceiling`` is the resolved lane-or-global number that fired, for
    the same reason its DISPATCH_TICK sibling carries it (#1751). The canonical
    9-field shape already varies per caller elsewhere (see ``renotify_marker``
    in reconcile/liveness.py), so one extra field here is additive.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": client_name,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
            "breadcrumbs": "",
            "crashed": False,
            "lane": lane,
            "attempt_ceiling": ceiling,
        },
        correlation_id=task.ticket_id,
    )


def _emit_stale_dispatch_blocked_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when the pre-dispatch open-PR gate parks a task.

    Sibling of :func:`_emit_attempt_cap_blocked_event` (#1862): per-task, not
    per-client-per-tick, so an operator can tell a loop that is quietly holding
    a stale row apart from a healthy idle loop.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.STALE_PR_BLOCKED,
            "ticket_id": ticket_id,
        },
    )


def _emit_worktree_occupied_skip_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when a ticket's worktree/hook-context is
    held by a live session or daemon worker (#2077).

    Sibling of _emit_stale_dispatch_blocked_event: per-task, outside the
    precedence chain, no SESSION_NEEDS_ATTENTION sibling -- nothing here
    needs operator action, the condition resolves itself. Shared by the
    pre-claim occupancy screen, the genuinely-live HookContextConflictError
    release, and the pre-existing WorktreeOccupiedError release.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.WORKTREE_OCCUPIED,
            "ticket_id": ticket_id,
        },
    )


def _emit_stale_dispatch_attention_event(
    task: TicketTask, client_name: str, lane: str
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for a pre-dispatch open-PR gate park (#1862).

    Mirrors :func:`_emit_attempt_cap_attention_event` exactly, including the
    canonical 9-field payload shape. ``paused_status`` is the *gate-suffixed*
    :data:`~cw.dev_queue.STALE_DISPATCH_GATE_DISPOSITION`, never the
    ``Status``-derived ``"stale_dispatch"``: no session ran for this park, so
    ``breadcrumbs`` is hardcoded empty and the literal must stay outside
    ``BREADCRUMB_ELIGIBLE_PAUSED_STATUSES`` (#1729 convention).
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": client_name,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": STALE_DISPATCH_GATE_DISPOSITION,
            "breadcrumbs": "",
            "crashed": False,
            "lane": lane,
        },
        correlation_id=task.ticket_id,
    )
