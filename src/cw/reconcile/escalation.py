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

from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus

# P1 (round-4 binding decision): a flat threshold, NOT per-stage. This is
# distinct from park-marker-poison's OWN transcript-staleness check inside
# cw.reconcile.concierge (which DOES use the per-stage
# _classify_liveness_bucket floor) — the two checks answer different
# questions and must not be conflated.
ESCALATION_PARK_MINUTES = 45

# Disposition branch: BLOCKED_ON_USER rows whose disposition is one of these
# 4 gates. "premises_pending_verification" is DELIBERATELY EXCLUDED — it is
# one of the ticket's named gates for approval purposes elsewhere, but was
# not included in the escalation-eligible formula per the binding round-4
# correction; do not add it back without re-opening that decision.
_ELIGIBLE_DISPOSITIONS: frozenset[str] = frozenset(
    {
        "ambiguities_pending_resolution",
        "plan_pending_approval",
        "review_pending_approval",
        # Added per round-2 A1: a ceiling-refused concierge recovery is
        # exactly the case that should surface to the operator.
        "stalled_retry_cap_parked",
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
