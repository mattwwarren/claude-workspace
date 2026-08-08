"""Durable operator-escalation latch (RFC 0008 capstone, GitHub #1015).

A gate that has sat parked for too long without operator action should be
paged exactly once — not on every reconcile tick, and not silently forever.
:func:`run_escalation_sweep` implements a 2-phase latch on
``TicketTask.escalation_parked_at`` / ``escalation_fired_at``:

1. **Enter**: a task newly in the escalation-eligible set (see
   :data:`_ELIGIBLE_DISPOSITIONS` / :data:`_ELIGIBLE_STATUSES` below) with no
   ``escalation_parked_at`` gets it stamped to *now* — this starts the clock.
2. **Fire**: once ``now - escalation_parked_at >= ESCALATION_PARK_MINUTES``
   and ``escalation_fired_at`` is still unset, emit exactly one
   :class:`~cw.models.OrchestratorEventType.OPERATOR_ESCALATION` event and
   stamp ``escalation_fired_at`` — the latch, so re-running this sweep every
   tick never re-fires for the same parked episode (mirrors the #996
   counter-shape precedent for "fire once, not once per tick").

Both fields are cleared together when the row leaves the parked state — that
clear-site lives in ``cw.dev_queue.transition_task_status`` (the single
mutation seam for every TicketTask status change), NOT here: this sweep only
ever *sets* the fields, never clears them, so a row can't have its clock
reset by anything other than actually leaving the eligible set.

Runs UNCONDITIONALLY every reconcile tick, regardless of
``OrchestratorConfig.concierge_enabled`` — durable escalation (this module)
is scoped independently of the mechanical recovery reactor
(``cw.reconcile.concierge``). Self-contained: acquires its own
``dev_queue_lock`` and never touches ``Session`` state, so it needs no
``sessions_lock`` and is safe to call standalone from ``cw watchdog tick``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.auto_dev_result import PAUSED_FOR_USER_INPUT_STATUSES
from cw.dev_queue import (
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile._shared import _REAP_ELIGIBLE_DISPOSITIONS_BASE

# P1 (round-4 binding decision): a flat threshold, NOT per-stage. This is
# distinct from park-marker-poison's OWN transcript-staleness check inside
# cw.reconcile.concierge (which DOES use the per-stage
# _classify_liveness_bucket floor) — the two checks answer different
# questions and must not be conflated.
ESCALATION_PARK_MINUTES = 45

# Disposition branch: BLOCKED_ON_USER rows whose disposition is one of these
# gates. Built from the same source constants dev_queue.py/concierge.py
# already use rather than re-typed literals, so the two can never silently
# drift apart. "premises_pending_verification" is DELIBERATELY EXCLUDED — it
# is one of the ticket's named gates for approval purposes elsewhere, but was
# not included in the escalation-eligible formula per the binding round-4
# correction; do not add it back without re-opening that decision.
#
# `None` is included per review follow-up: recipe 1 (false_park_requeue in
# cw.reconcile.concierge) treats a null-disposition BLOCKED_ON_USER row (the
# idle-watchdog's silently-idle park) as ceiling-refusable exactly like
# stalled_retry_cap_parked. Without it, a ceiling-refused null-disposition row
# is invisible to both concierge and escalation — a silent stuck row, the
# exact failure mode round-2's A1 added stalled_retry_cap_parked to kill. A1's
# own reasoning ("a concierge-declined recovery... is the highest-value case
# for surfacing to the operator") applies identically here.
# #976: fixing the null-disposition park bug means idle.py's silently-idle
# park now stamps `_SILENTLY_IDLE_REASON` instead of `None`, and the
# stalled/idle/phantom SIGNAL_ONLY reroute + park paths now stamp
# ReapReason.IDLE_STALL / WALL_CLOCK_BUDGET / PHANTOM_SURFACE instead of
# leaving disposition null. Escalation eligibility must follow those
# dispositions or escalation coverage regresses for those park classes —
# a ceiling-refused row with one of these reasons is exactly the "silent
# stuck row" case `None` was already included here to catch.
#
# GitHub #1571: the 6-member reap-eligible base now lives in
# _shared._REAP_ELIGIBLE_DISPOSITIONS_BASE (imported above) -- it was
# hand-typed identically here and in concierge.py's
# _FALSE_PARK_ELIGIBLE_DISPOSITIONS, synced only by a comment telling the
# reader to update both. Only the value moved; this module's own addition
# (PAUSED_FOR_USER_INPUT_STATUSES minus the named exclusion) is unchanged.
#
# GitHub #1702: the review-health gate joins as a THIRD union term, deliberately
# not by extending _REAP_ELIGIBLE_DISPOSITIONS_BASE -- that shared frozenset also
# feeds concierge's _FALSE_PARK_ELIGIBLE_DISPOSITIONS, and auto-requeuing a
# review-health park would defeat the gate (see concierge.py's import site).
# Escalation-eligible it *is*, though: it is an unresolved, non-operator-
# initiated quality signal, the same class as the plan/review_pending_approval
# members above -- not a deliberately-armed operator stop (signoff_gate,
# finalize_gate_held), which are intentionally absent from this set because
# their clock does not start until an operator chooses to act.
#
# GitHub #1714 joins as a FOURTH union term on identical reasoning to #1702's:
# a mechanically-rejected MUST_FIX park is an unresolved, non-operator-
# initiated quality signal, so its escalation clock starts immediately. Also
# kept out of _REAP_ELIGIBLE_DISPOSITIONS_BASE for the same reason -- concierge
# auto-requeuing it would misread a dropped finding as a session glitch.
_ELIGIBLE_DISPOSITIONS: frozenset[str | None] = frozenset(
    (PAUSED_FOR_USER_INPUT_STATUSES - {"premises_pending_verification"})
    | _REAP_ELIGIBLE_DISPOSITIONS_BASE
    | {
        REVIEW_HEALTH_GATE_DISPOSITION,
        REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    }
)

# Status branch: disposition is irrelevant for these two statuses.
_ELIGIBLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, QueueItemStatus.FAILED}
)


def _is_escalation_eligible(
    task_status: QueueItemStatus, disposition: str | None
) -> bool:
    """Two-branch escalation-eligible formula (binding, round-4 correction).

    NOT a single disposition-set: a BLOCKED_ON_USER row is eligible only for
    4 specific dispositions, while AWAITING_OPERATOR_SIGNOFF/FAILED rows are
    eligible regardless of disposition.
    """
    if task_status == QueueItemStatus.BLOCKED_ON_USER:
        return disposition in _ELIGIBLE_DISPOSITIONS
    return task_status in _ELIGIBLE_STATUSES


def run_escalation_sweep(*, now: datetime | None = None) -> list[str]:
    """Stamp/fire the durable escalation latch for every eligible task.

    Returns the list of ticket IDs for which ``OPERATOR_ESCALATION`` was
    newly fired this call (empty when nothing crossed the threshold).
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    fired: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if not _is_escalation_eligible(task.status, task.disposition):
                continue
            if task.escalation_parked_at is None:
                task.escalation_parked_at = resolved_now
                changed = True
                continue
            if task.escalation_fired_at is not None:
                continue
            elapsed_minutes = (
                resolved_now - task.escalation_parked_at
            ).total_seconds() / 60.0
            if elapsed_minutes < ESCALATION_PARK_MINUTES:
                continue
            record_event(
                OrchestratorEventType.OPERATOR_ESCALATION,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "status": task.status,
                    "disposition": task.disposition,
                    "lane": task.lane,
                    "stage": task.stage,
                    "parked_at": task.escalation_parked_at.isoformat(),
                    "elapsed_minutes": elapsed_minutes,
                },
                correlation_id=task.ticket_id,
            )
            task.escalation_fired_at = resolved_now
            fired.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return fired
