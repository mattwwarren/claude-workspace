"""Shared labels, purposes and command builders for the rule table.

The DAG root of ``cw.dev_queue.next_commands`` (#2999): every rule
submodule builds its commands from these, and nothing here imports a
sibling.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_LARGE,
)
from cw.dev_queue.dispositions import (
    FINALIZE_GATE_HELD_DISPOSITION,
)
from cw.dev_queue.gates import (
    APPROVAL_GATE_REASON,
)
from cw.models import QueueItemStatus, Stage
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


# Operator-supplied values: the only places a command is not runnable as-is.
SCOPE_DRIFT_PATHS_PLACEHOLDER = "<paths>"
_REASON_PLACEHOLDER = '"<why>"'

HUMAN_GATE_LABEL = "operator-only human gate; not for an agent to run"
HUMAN_GATE_PURPOSE_LABEL = f"{HUMAN_GATE_LABEL}: "
HUMAN_GATE_CELL_LABEL = "HUMAN GATE: "
# Why a #2760 requeue replaces approve: the terminal-stage guard, or no bound
# session at the approval gate (session not found / not at an approval gate).
_TERMINAL_REFUSES = "approve refuses at the terminal stage"
_NO_GATE_SESSION = (
    "approve cannot take this row because no bound session is at the approval gate"
)
_RERUN_CURRENT = "re-run the current stage"
_FINALIZE_HOLD_GATE = "finalize-hold"
# Rule 0c/0d's purpose text and predicates live in ``finalize_park``.
# Statuses an off-pipeline row is listed `show` + plain `requeue` for: both
# are refused by `approve` and `requeue --stage`, and accepted by plain
# `requeue` (crud._APPROVABLE_STATUSES).
_OFF_PIPELINE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {QueueItemStatus.BLOCKED_ON_USER, QueueItemStatus.AWAITING_OPERATOR_SIGNOFF}
)
_SHIP_STEP_HUMAN_CAVEAT = (
    "if the ticket or lane has a finalize hold, operator signoff or"
    " scope_hint: large, this step is the human's."
)
_SHIP_STEP_IS_HUMANS = (
    "it ships nothing, and shipping this ticket past review is the human's step"
    " because the ticket or its lane carries a finalize hold, operator signoff"
    " or scope_hint: large"
)
_SHOW_PURPOSE = "inspect the row and its legal next commands"

# A scope_hint: large gate is recorded as one of these dispositions: the
# REVIEW->FINALIZE scope-hint park stamps APPROVAL_GATE_REASON, a Rule 1 park
# stamps the verbatim sentinel status (dispositions._derive_disposition).
_SCOPE_HINT_GATE_DISPOSITIONS: frozenset[str] = SCOPE_GATED_APPROVAL_STATUSES | {
    APPROVAL_GATE_REASON
}

_FROM_FLAGS: dict[QueueItemStatus, str] = {
    QueueItemStatus.COMPLETED: "--from-completed",
    QueueItemStatus.FAILED: "--from-failed",
    QueueItemStatus.CANCELLED: "--from-cancelled",
}


def dev_queue_command(verb: str, task: TicketTask, *extra: str) -> str:
    """``cw dev-queue <verb> <ticket> -c <client> [extra...]``, shell-quoted."""
    return " ".join(
        [
            "cw",
            "dev-queue",
            verb,
            shlex.quote(task.ticket_id),
            "-c",
            shlex.quote(task.client),
            *extra,
        ]
    )


def is_human_kept_gate(task: TicketTask) -> bool:
    """True iff *task* is parked at a gate the human opted into.

    An operator-signoff park, a finalize hold (``hold_finalize`` /
    ``finalize_gate: manual``), or a ``scope_hint: large`` approval gate. A
    large-by-size gate without ``scope_hint: large`` is not one of these, and
    neither is a ``scope_hint: large`` row parked for any other reason.
    """
    if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        return True
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        return False
    if task.disposition == FINALIZE_GATE_HELD_DISPOSITION:
        return True
    return (
        task.scope_hint == SCOPE_TIER_LARGE
        and task.disposition in _SCOPE_HINT_GATE_DISPOSITIONS
    )


def row_carries_human_opt_in(task: TicketTask) -> bool:
    """True iff the row itself opts the ticket into a human gate.

    ``scope_hint: large`` (exact string), ``hold_finalize: manual`` or
    ``signoff: operator``. Lane and global opt-ins are not on the row, so this
    is the partial, pure check; ``show``/``tasks`` resolve the rest.
    """
    return (
        task.scope_hint == SCOPE_TIER_LARGE
        or task.hold_finalize == "manual"
        or task.signoff == "operator"
    )


def _row_state(task: TicketTask) -> str:
    """``<status> at stage <stage>[ (disposition '<d>')]``."""
    disposition = f" (disposition '{task.disposition}')" if task.disposition else ""
    return f"{task.status.value} at stage {task.stage.value}{disposition}"


def human_gate_settled_notice(task: TicketTask) -> str:
    """The refusal ``approve`` raises after settling a row into a human gate."""
    return (
        f"Cannot approve ticket '{task.ticket_id}': its recorded sentinel was just"
        " routed and the row now sits at a human-kept gate: it is"
        f" {_row_state(task)}. Nothing was released. {HUMAN_GATE_PURPOSE_LABEL}"
        "the human operator reviews it with"
        f" `{dev_queue_command('show', task)}` and releases it with a second"
        f" `{dev_queue_command('approve', task)}`."
    )


def _requeue(
    task: TicketTask, purpose: str, *extra: str, operator_only: bool = False
) -> NextCommand:
    return NextCommand(
        command=dev_queue_command("requeue", task, *extra),
        purpose=purpose,
        operator_only=operator_only,
    )


def _stage_flag(task: TicketTask) -> tuple[str, ...]:
    """``--stage <current stage>``; HARDEN is not a ``--stage`` choice.

    ``requeue``'s own default (the current stage) is the same move.
    """
    if task.stage is Stage.HARDEN:
        return ()
    return ("--stage", task.stage.value)


def _requeue_at_stage(task: TicketTask, purpose: str | None = None) -> NextCommand:
    """``requeue --stage <current stage>``, by default "re-run ... instead"."""
    text = (
        purpose
        if purpose is not None
        else f"re-run the {task.stage.value} stage instead"
    )
    return _requeue(task, text, *_stage_flag(task))


def _human_requeue(task: TicketTask, purpose_body: str, *extra: str) -> NextCommand:
    """A human-kept gate's requeue: labelled and flagged ``operator_only``."""
    return _requeue(
        task, f"{HUMAN_GATE_PURPOSE_LABEL}{purpose_body}", *extra, operator_only=True
    )


def _human_gate_name(task: TicketTask) -> str:
    """The gate a terminal-stage human requeue releases (P2/P3T ``<gate>``)."""
    if task.disposition == FINALIZE_GATE_HELD_DISPOSITION:
        return _FINALIZE_HOLD_GATE
    return f"{task.stage.value} approval"


def _rerun(task: TicketTask) -> NextCommand:
    return _requeue(task, f"re-run the {task.stage.value} stage")


def _approve(
    task: TicketTask, purpose: str, *extra: str, operator_only: bool = False
) -> NextCommand:
    return NextCommand(
        command=dev_queue_command("approve", task, *extra),
        purpose=purpose,
        operator_only=operator_only,
    )


def _human_approve(task: TicketTask, purpose_body: str) -> NextCommand:
    """A human-kept gate's plain approve: labelled and flagged ``operator_only``."""
    return _approve(
        task, f"{HUMAN_GATE_PURPOSE_LABEL}{purpose_body}", operator_only=True
    )


def _wait(task: TicketTask) -> NextCommand:
    return NextCommand(
        command=dev_queue_command("wait", task),
        purpose="block until the ticket finishes or parks",
    )


def _override_must_fix(task: TicketTask, purpose: str) -> NextCommand:
    return _approve(
        task, purpose, "--override-must-fix", "--reason", _REASON_PLACEHOLDER
    )


def _ship_under_override(task: TicketTask, purpose: str) -> NextCommand:
    return _requeue(
        task, f"{purpose}; {_SHIP_STEP_HUMAN_CAVEAT}", "--stage", Stage.FINALIZE.value
    )


def _is_blocked(task: TicketTask) -> bool:
    return task.status == QueueItemStatus.BLOCKED_ON_USER


def _anything(_task: TicketTask, _session: Session | None) -> bool:
    return True


def _off_pipeline_commands(task: TicketTask) -> list[NextCommand]:
    """``show`` first, then the plain ``requeue`` (R12), labelled for a human gate."""
    show = NextCommand(
        command=dev_queue_command("show", task),
        purpose=(
            f"stage {task.stage.value} is not in this row's lane pipeline, so"
            " approve and requeue --stage refuse it until the operator adds it to"
            " pipeline.stages or removes and re-adds the ticket; inspect the row"
        ),
    )
    requeue = (
        _human_requeue(task, _RERUN_CURRENT)
        if is_human_kept_gate(task)
        else _requeue(task, _RERUN_CURRENT)
    )
    return [show, requeue]


def _generic_commands(task: TicketTask) -> list[NextCommand]:
    return [_rerun(task)]
