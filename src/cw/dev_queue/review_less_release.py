"""Operator release of a finalize Step 4.0 park on a lane with no review stage (#2779).

A large-tier row parks at finalize Step 4.0 until it carries a review approval
valid for HEAD. On a lane whose pipeline has a review stage that approval is
recorded at REVIEW, so the park is released by ``requeue --stage review``. On a
lane without one that requeue is refused, and ``approve`` used to be refused at
the terminal stage as well, leaving the row with no legal release.

This module is that release. The operator's plain ``cw dev-queue approve``
stamps ``approver_kind="operator"`` on the row, bound to the worktree's HEAD
at approve time (the park sentinel records no head, so there is nothing more
precise to bind to; #3015 tracks the stronger form). The row keeps its status
and stage; the operator then runs ``requeue --stage finalize``, whose Step 4.0
check re-evaluates the approval against the branch head.

Stamp-only, like ``must_fix_override`` and ``scope_drift``. The caller holds
the dev-queue lock and has already checked eligibility
(``approval._try_review_less_release``); this module never imports
``approval``. HEAD is read before anything is written and an unreadable HEAD
refuses; the ``TICKET_APPROVED`` audit event is recorded before the save, so a
failed event leaves no stamp on disk.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.branch_ahead import current_head_sha
from cw.dev_queue.finalize_park import is_approval_park
from cw.dev_queue.lifecycle import _record_review_approval, _review_approval_snapshot
from cw.dev_queue.storage import save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import OrchestratorEventType
from cw.models.enums import PipelinePosition
from cw.operator_identity import resolve_actor_or_empty
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore, Session, TicketTask

_log = logging.getLogger(__name__)

REVIEW_LESS_RELEASE_REASON: Final = "review_less_lane_release"


def is_review_less_release(
    task: TicketTask,
    session: Session,
    position: PipelinePosition,
    *,
    operator_initiated: bool,
) -> bool:
    """True iff this ``approve`` is the review-less release of *task*.

    Only a human ``approve`` (*operator_initiated*; never the gate recipe,
    ``drain`` or any automatic caller) of a finalize Step 4.0 approval park,
    not a Step 4.0b stale-tip park, at the last stage of a lane with no review
    stage. Every other row falls through to the terminal refusal.
    """
    return (
        operator_initiated
        and position is PipelinePosition.TERMINAL_NO_REVIEW
        and is_approval_park(task, session)
    )


def _row_head(task: TicketTask, client_cfg: ClientConfig) -> str | None:
    """*task*'s worktree HEAD, or None when it cannot be read.

    The only git read of the release, and the test seam.
    """
    return current_head_sha(resolve_task_worktree(task, client_cfg))


def release_review_less_park(
    store: DevQueueStore,
    task: TicketTask,
    client_name: str,
    client_cfg: ClientConfig,
) -> str:
    """Stamp an operator review approval bound to *task*'s HEAD; return the head.

    The caller MUST hold ``dev_queue_lock()`` and has established that *task*
    sits at a finalize Step 4.0 park on a lane with no review stage and that a
    human ran ``approve``. *store* holds *task* and is saved here.

    Raises:
        ApproveGateError: when the worktree HEAD cannot be read; nothing is
            recorded. A failing audit event propagates with nothing saved.
    """
    ticket_id = task.ticket_id
    head = _row_head(task, client_cfg)
    if head is None:
        msg = (
            f"Cannot approve ticket '{ticket_id}': the worktree HEAD could not be"
            " read, so there is no branch head to bind the approval to. Nothing"
            " was recorded. Check that the ticket's worktree exists and is a git"
            " checkout, then approve again."
        )
        raise ApproveGateError(msg)
    actor = resolve_actor_or_empty(
        client_cfg, context=f"review-less release for {client_name}/{ticket_id}"
    )
    stage = task.stage.value
    before = _review_approval_snapshot(task)
    _record_review_approval(task, "operator", head, datetime.now(UTC))
    record_event(
        OrchestratorEventType.TICKET_APPROVED,
        {
            "ticket_id": ticket_id,
            "client": client_name,
            "from_stage": stage,
            "to_stage": stage,
            "actor": actor,
            "reason": REVIEW_LESS_RELEASE_REASON,
            "reviewed_sha": head,
            "review_approver_kind": "operator",
            "review_approval_before": before,
            "review_approval_after": _review_approval_snapshot(task),
        },
        correlation_id=ticket_id,
    )
    try:
        save_dev_queue(store)
    except Exception:
        _log.error(
            "review-less release save failed for %s/%s after TICKET_APPROVED was"
            " recorded; the row carries no new approval, so finalize still parks",
            client_name,
            ticket_id,
            exc_info=True,
        )
        raise
    return head
