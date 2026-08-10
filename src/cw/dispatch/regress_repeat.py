"""#1717: FINALIZE self-heal regress round-trip repeat detection.

Closes the silent-repeat-park incident (#1644, #1702, #1710): a FINALIZE
self-heal regress (#770, ``dev_queue.lifecycle._stage_regress``) reverts a
task to ``Stage.IMPL`` for self-heal. If that IMPL leg produces no new
commit, the task walks IMPL->REVIEW and REVIEW's scope-gated gates
(``dispatch.routing``) re-evaluate from a blank slate, re-parking the ticket
with an identical disposition and burning an attempt each cycle -- with no
operator-visible signal that this is a *repeat*, not a fresh park.

``_stage_regress`` stamps ``TicketTask.finalize_regress_branch_head`` with
the pre-regress ``stage_base_ref`` (the branch-head oracle) whenever it
regresses FROM ``Stage.FINALIZE``. This module owns the REVIEW-side
consumption of that marker: :func:`_consume_finalize_regress_repeat` reads
and clears it, comparing the stored SHA to the freshly-restamped
``stage_base_ref`` at REVIEW's next claim; :func:`_maybe_emit_finalize_
regress_repeat_signal` fires a companion ``SESSION_NEEDS_ATTENTION`` event
when that comparison found a repeat AND this pass re-parked the task (never
when it advanced past REVIEW instead).

Kept as a sibling module to ``dispatch.routing`` rather than folded into it
(#1728: routing.py is already 1445 lines) -- imported by routing.py and
re-exported through ``cw.dispatch.__init__``, following the package-split
convention the ``cw.dispatch`` package already uses (#1310-1312).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from cw.models import TicketTask

# paused_status written to the #1717 companion SESSION_NEEDS_ATTENTION event.
# Distinct from every existing gate's paused_status (_APPROVAL_GATE_REASON,
# _FINALIZE_HOLD_REASON, _SIGNOFF_GATE_REASON, _REVIEW_HEALTH_GATE_REASON,
# routing.py): this signal rides ALONGSIDE whichever of those the ordinary
# park already emitted, as a second, independent event -- it does not
# replace or reclassify the ordinary park.
_FINALIZE_REGRESS_REPEAT_REASON = "finalize_regress_repeat"


def _consume_finalize_regress_repeat(task: TicketTask) -> bool:
    """Read-and-clear ``task.finalize_regress_branch_head``; True iff a repeat.

    Callers MUST scope this to ``task.stage == Stage.REVIEW`` -- mirroring the
    existing convention on ``dispatch.routing._should_gate_for_review_health``
    -- and must call it at most once per REVIEW-stage routing pass, since the
    marker is cleared as a side effect regardless of the outcome.

    Returns ``False`` when no marker is set (no FINALIZE-origin regress is in
    play). Otherwise clears the marker and returns whether the stored
    pre-regress branch head equals ``task.stage_base_ref`` as freshly
    restamped by REVIEW's claim -- ``True`` means zero commits landed
    anywhere in the finalize->impl->review round trip, the exact condition
    that produced the #1644/#1702/#1710 silent-repeat incidents.
    """
    marker = task.finalize_regress_branch_head
    task.finalize_regress_branch_head = None
    if marker is None:
        return False
    return marker == task.stage_base_ref


def _maybe_emit_finalize_regress_repeat_signal(
    task: TicketTask, is_repeat: bool
) -> None:
    """Emit the #1717 companion SESSION_NEEDS_ATTENTION signal iff this pass's
    REVIEW-scoped gate re-parked the task with a branch-head-unchanged
    finalize regress in play.

    No-ops in two cases: (a) ``is_repeat`` is ``False`` (no marker was set, or
    the branch-head comparison did not match -- a real commit landed); (b)
    this pass advanced the task past REVIEW instead of parking it
    (``task.status`` is neither ``BLOCKED_ON_USER`` nor
    ``AWAITING_OPERATOR_SIGNOFF`` at this point) -- the loop broke this round,
    so there is nothing to surface. ``task.disposition`` is read, never
    written, here: the ordinary park (whichever of the four REVIEW-scoped
    gates fired) already stamped it before this call runs.
    """
    if not is_repeat:
        return
    if task.status not in (
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ):
        return
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _FINALIZE_REGRESS_REPEAT_REASON,
            "breadcrumbs": (
                f"attempts={task.regress_attempts} "
                f"branch_head={task.stage_base_ref!r} "
                f"pr_url={task.pr_url!r} "
                f"disposition={task.disposition!r}"
            ),
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
