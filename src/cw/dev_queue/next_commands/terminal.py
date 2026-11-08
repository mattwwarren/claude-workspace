"""The red-shipped and terminal-status rules (3a, 4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue.next_commands._common import (
    _FROM_FLAGS,
    _requeue,
    dev_queue_command,
)
from cw.dev_queue.shipped_red import (
    RECOVER_RED_PR_VERB,
    is_red_shipped_row,
)
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


def red_recover_command(task: TicketTask) -> str:
    """``cw dev-queue recover-red-pr <T> -c <C>`` (#2945)."""
    return dev_queue_command(RECOVER_RED_PR_VERB, task)


def _red_shipped(task: TicketTask, _session: Session | None) -> bool:
    return is_red_shipped_row(task)


def _red_shipped_commands(task: TicketTask) -> list[NextCommand]:
    purpose = (
        "disarm auto-merge on the shipped PR and regress this row to impl with"
        " head-pinned CI-failure feedback; finalize re-arms it after review"
        " (not --from-completed)"
    )
    return [NextCommand(command=red_recover_command(task), purpose=purpose)]


def _terminal(task: TicketTask, _session: Session | None) -> bool:
    return task.status in _FROM_FLAGS


def _terminal_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _requeue(
            task,
            f"recover the {task.status.value} row to PENDING at its"
            f" {task.stage.value} stage",
            _FROM_FLAGS[task.status],
        )
    ]
