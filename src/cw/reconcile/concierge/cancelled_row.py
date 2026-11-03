"""Recipe 3: cancelled-row dead-end normalization.

A CANCELLED row whose worktree still has committed work ahead of its base
branch is restored to PENDING so that work is not silently lost. The git
answers come from :class:`~cw.reconcile.cancelled_row_probes.CancelledRowProbes`
captured before the lock (#2546). See the package docstring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile.cancelled_row_probes import (
    cancelled_row_has_commits,
    cancelled_row_targets,
)
from cw.reconcile.concierge._common import (
    RECIPE_CANCELLED_ROW_RESTORE,
    ConciergeCandidate,
)

if TYPE_CHECKING:
    from cw.models import TicketTask
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes


# ---------------------------------------------------------------------------
# Recipe 3: cancelled-row dead-end normalization
# ---------------------------------------------------------------------------


def _detect_cancelled_row_candidates(
    tasks: list[TicketTask], probes: CancelledRowProbes | None
) -> list[ConciergeCandidate]:
    """Pure classification phase for recipe 3. Makes zero writes, runs no git.

    No attempt-ceiling gate (per the ticket's one-time reasoned omission —
    this recovers committed work, it is not a retry) and no ``attempts``
    increment on restore (A3): the next real claim increments as usual.

    Why this also restores operator-initiated cancels, not just mechanical
    ones: the ticket's own deliverable text names the target population as
    "operator/spawn-close-produced `cancelled` rows with a live worktree +
    committed work" — both sources are explicitly in scope by design. There is
    deliberately no field distinguishing "operator meant to cancel this for
    good" from "a mechanical process cancelled it in error": the ticket's
    stated intent is that committed work should never be silently lost to
    *any* cancel, of either origin. An operator who wants a ticket to stay
    cancelled despite committed work should set
    ``concierge_recoveries.cancelled_row_restore: false`` (Q7) or clear the
    worktree; this was reviewed and confirmed against the ticket text as
    intentional, not a defect.
    """
    candidates: list[ConciergeCandidate] = []
    for key in cancelled_row_targets(tasks):
        if not cancelled_row_has_commits(probes, key):
            continue
        candidates.append(
            ConciergeCandidate(
                ticket_id=key.ticket_id,
                client=key.client,
                recipe=RECIPE_CANCELLED_ROW_RESTORE,
                evidence={
                    "worktree_path": str(key.worktree_path),
                    "default_branch": key.default_branch,
                },
            )
        )
    return candidates


def _act_on_cancelled_row_candidates(candidates: list[ConciergeCandidate]) -> list[str]:
    """Act phase for recipe 3: emit-then-restore CANCELLED rows to PENDING.

    ADR-0006: non-destructive. Restoring to PENDING never touches the
    worktree or stops any process — the detect phase already confirmed the
    worktree exists and has committed work ahead of its base branch.
    """
    if not candidates:
        return []
    by_ticket = {c.ticket_id: c for c in candidates}
    recovered: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            candidate = by_ticket.get(task.ticket_id)
            if candidate is None:
                continue
            if task.status != QueueItemStatus.CANCELLED:
                continue
            record_event(
                OrchestratorEventType.CONCIERGE_RECOVERED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "recipe": RECIPE_CANCELLED_ROW_RESTORE,
                    "evidence": candidate.evidence,
                },
                correlation_id=task.ticket_id,
            )
            transition_task_status(task, QueueItemStatus.PENDING)
            recovered.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return recovered
