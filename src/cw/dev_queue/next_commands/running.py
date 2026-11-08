"""The PENDING and RUNNING rules (2, 3) and the paused-lane remedy."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.dev_queue.next_commands._common import (
    _wait,
    dev_queue_command,
)
from cw.models import DispatchSkipReason, QueueItemStatus
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


# Synthetic skip code for a PENDING row whose lane is held paused by an
# operator, a legacy pause or ``clients.yaml`` -- not a ``DispatchSkipReason``
# (dispatch reports no reason for it), but the ``cw dev-queue wait`` /
# ``cw orchestrate watch`` stuck-pending vocabulary names it (#2693).
LANE_PAUSED_SKIP_CODE = "lane_paused"
LANE_RESUME_PURPOSE = "resume the paused lane so dispatch can claim the ticket"
# PENDING skip codes whose remedy is the lane's ``cw lane resume`` (#2693).
# Every other skip code has no ``cw`` remedy on a PENDING row:
# ``requeue --reset-attempts`` is a no-op there, and the ssh-key, disk and
# screening gates are fixed outside cw.
_LANE_RESUME_SKIP_CODES: frozenset[str] = frozenset(
    {DispatchSkipReason.LANE_CIRCUIT_PAUSED.value, LANE_PAUSED_SKIP_CODE}
)


def _pending(task: TicketTask, _session: Session | None) -> bool:
    return task.status == QueueItemStatus.PENDING


def _running(task: TicketTask, _session: Session | None) -> bool:
    return task.status == QueueItemStatus.RUNNING


def _pending_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _wait(task),
        NextCommand(
            command=dev_queue_command("cancel", task),
            purpose="drop it from the queue",
        ),
    ]


def _running_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _wait(task),
        NextCommand(
            command=f"cw queue peek -c {shlex.quote(task.client)}",
            purpose="inspect the running session (age, idle gap, last result)",
        ),
    ]
