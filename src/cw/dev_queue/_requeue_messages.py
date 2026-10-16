"""Refusal-message builders for ``cw.dev_queue.requeue`` (#2692).

Split out of ``requeue.py`` to keep that module under the ~1000-line ceiling
once every requeue refusal named its legal next command. Imports nothing from
``requeue`` (so no cycle); ``requeue`` re-imports every name here, so its
module attributes are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue.next_commands import dev_queue_command
from cw.models import QueueItemStatus, Stage

if TYPE_CHECKING:
    from cw.models import TicketTask

# Issue #917: --regress requires an explicit backward --stage target.
_REGRESS_NEEDS_BACKWARD_STAGE_MSG = (
    "--regress requires a backward --stage target on a blocked task."
)
_UNBLOCK_SALVAGE_ONLY = (
    " unblock applies to salvage parks only (the bound session's reap_reason"
    " must be SALVAGE_PARKED)."
)

# Statuses requeue_ticket's forward/same-stage path additionally accepts, but
# ONLY when the caller explicitly opts in via from_cancelled=True (CLI:
# --from-cancelled). Deliberately NOT folded into _APPROVABLE_STATUSES, which
# is shared by _find_ticket's tie-break, approve_ticket, and the regress gate
# in _apply_requeue_stage — widening that frozenset would silently change
# behavior at all three. See GitHub #1018: `cw spawn close <sid>
# --confirmed-dead` on a RUNNING row transitions it to CANCELLED (not
# BLOCKED_ON_USER), leaving no CLI path back to PENDING.
_REQUEUE_FROM_CANCELLED_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.CANCELLED]
)

# Mirrors _REQUEUE_FROM_CANCELLED_STATUSES above for FAILED/abandoned rows
# whose underlying session may have actually completed clean (a valid
# terminal sentinel) but the row itself has no requeue path today. Same
# "deliberately NOT folded into _APPROVABLE_STATUSES" rationale -- this is
# a blind operator-trust escape hatch (CLI: --from-failed), not automatic
# sentinel re-verification. See GitHub #1190.
_REQUEUE_FROM_FAILED_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.FAILED]
)

# Mirrors _REQUEUE_FROM_CANCELLED_STATUSES / _REQUEUE_FROM_FAILED_STATUSES
# above for COMPLETED rows: a ticket that shipped and landed can later go
# conflicting because a sibling ticket in the same wave merged first,
# leaving no CLI path back to PENDING short of deleting and re-adding the
# row (which would lose its attempt/cost history). Same "deliberately NOT
# folded into _APPROVABLE_STATUSES" rationale -- this is a blind
# operator-trust escape hatch (CLI: --from-completed), not automatic
# re-verification of the shipped PR's state. See GitHub #2023.
_REQUEUE_FROM_COMPLETED_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.COMPLETED]
)


def _pending_restage_hint(task: TicketTask, stage_override: str | None) -> str:
    """How to move a PENDING row to another stage; "" for any other request."""
    if (
        task.status != QueueItemStatus.PENDING
        or stage_override is None
        or stage_override == task.stage.value
    ):
        return ""
    remove = dev_queue_command("remove", task)
    add = dev_queue_command("add", task, "--stage", stage_override)
    return (
        " To change the stage of a PENDING row, remove it and re-add it with"
        f" --stage: `{remove}`, then `{add}`."
    )


def _regress_needs_stage_message(task: TicketTask, stages: list[Stage]) -> str:
    """The ``--regress`` without ``--stage`` refusal, naming an example."""
    current = stages.index(task.stage) if task.stage in stages else 0
    earlier = [s for s in stages[:current] if s is not Stage.HARDEN]
    if not earlier:
        return (
            f"{_REGRESS_NEEDS_BACKWARD_STAGE_MSG} For this row there is no earlier"
            f" stage than '{task.stage.value}' to regress to."
        )
    example = dev_queue_command(
        "requeue", task, "--stage", earlier[-1].value, "--regress"
    )
    return (
        f"{_REGRESS_NEEDS_BACKWARD_STAGE_MSG} Name the earlier stage, e.g."
        f" `{example}` (current stage: '{task.stage.value}')."
    )


def _requeue_state_error_message(ticket_id: str, status: QueueItemStatus) -> str:
    """Build the RequeueStateError message for the forward/same-stage gate.

    Names the --from-cancelled, --from-failed, or --from-completed escape
    hatch only when it would actually apply (status is CANCELLED, FAILED, or
    COMPLETED, respectively) so the message doesn't mislead callers hitting
    the gate from PENDING/RUNNING/etc. See GitHub #1018, #1190, #2023.
    """
    base = (
        f"Cannot requeue ticket '{ticket_id}':"
        f" status is {status.value!r},"
        " expected BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF"
    )
    if status in _REQUEUE_FROM_CANCELLED_STATUSES:
        return base + " (or CANCELLED with --from-cancelled)."
    if status in _REQUEUE_FROM_FAILED_STATUSES:
        return base + " (or FAILED with --from-failed)."
    if status in _REQUEUE_FROM_COMPLETED_STATUSES:
        return base + " (or COMPLETED with --from-completed)."
    return base + "."
