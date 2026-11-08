"""``next_commands`` and the refusal / attention helpers built on it.

Walks ``table._RULES`` for a row and adapts the matched rule to the
row's pipeline position and opt-in.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.dev_queue.next_commands._common import (
    _OFF_PIPELINE_STATUSES,
    _SHOW_PURPOSE,
    HUMAN_GATE_CELL_LABEL,
    _is_blocked,
    _off_pipeline_commands,
    _row_state,
    dev_queue_command,
    is_human_kept_gate,
    row_carries_human_opt_in,
)
from cw.dev_queue.next_commands.running import (
    _LANE_RESUME_SKIP_CODES,
    LANE_RESUME_PURPOSE,
)
from cw.dev_queue.next_commands.table import (
    _RULES,
    _Rule,
)
from cw.dev_queue.park_guidance import PARK_GUIDANCE
from cw.models import QueueItemStatus
from cw.models.enums import PipelinePosition
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


# A human gate's releasing verbs (R15): the row-state half of
# is_human_gate_command.
_RELEASE_VERBS: frozenset[str] = frozenset({"approve", "requeue"})


def _position_commands(
    rule: _Rule,
    task: TicketTask,
    position: PipelinePosition,
    session: Session | None,
) -> list[NextCommand] | None:
    """The commands a non-interior *position* substitutes, or None (#2760).

    Off the pipeline, ``approve`` and ``requeue --stage`` both refuse a
    BLOCKED or signoff row, so it lists ``show`` and the plain ``requeue``. At
    the last stage of a lane with no review stage a BLOCKED row's rule lists
    its ``no_review_commands`` when it has them (#2779). At either terminal
    position a BLOCKED row's rule otherwise lists its ``terminal_commands``
    (``approve`` refuses there); a signoff row stays approvable.
    """
    if (
        position is PipelinePosition.OFF_PIPELINE
        and task.status in _OFF_PIPELINE_STATUSES
    ):
        return _off_pipeline_commands(task)
    if not (position.is_terminal and _is_blocked(task)):
        return None
    if (
        position is PipelinePosition.TERMINAL_NO_REVIEW
        and rule.no_review_commands is not None
    ):
        return rule.no_review_commands(task, session)
    if rule.terminal_commands is not None:
        return rule.terminal_commands(task)
    return None


def next_commands(
    task: TicketTask,
    *,
    session: Session | None,
    operator_opt_in: bool = False,
    skip_reason: str | None = None,
    lane_resume_command: str | None = None,
    pipeline_position: PipelinePosition = PipelinePosition.INTERIOR,
    finalize_in_pipeline: bool = True,
) -> list[NextCommand]:
    """The legal next commands for *task*, most useful first.

    *session* is the cw ``Session`` *task* is bound to, or None when it has
    none or it is gone; see the module docstring for how to resolve it.
    *operator_opt_in* is the config-resolved human opt-in (lane or global
    finalize hold / signoff) the row cannot show; it only affects rules 6/6a.
    *pipeline_position* is the row stage's caller-resolved position in its
    lane pipeline (``cw.executor.pipeline_position``); the default,
    ``INTERIOR``, leaves the output unchanged. *finalize_in_pipeline* is
    whether FINALIZE is in that pipeline (``cw.executor.finalize_in_pipeline``,
    #2765); when False, rules 6/6a list a plain re-run instead of
    ``requeue --stage finalize``, and the default, True, leaves the output
    unchanged.

    *skip_reason* is why a PENDING row is not dispatching (a
    ``DispatchSkipReason`` value or :data:`LANE_PAUSED_SKIP_CODE`) and
    *lane_resume_command* the row's lane ``LanePauseView.resume_command``.
    For a PENDING row paused by its lane (``lane_circuit_paused`` or
    ``lane_paused``) with a resume command supplied, that command leads the
    list (#2693). No other skip code prepends anything, and every other row
    ignores both arguments, so the output is unchanged when *skip_reason* is
    None.
    """
    rule = next(r for r in _RULES if r.matches(task, session))
    positioned = _position_commands(rule, task, pipeline_position, session)
    if positioned is not None:
        return positioned
    if rule.session_commands is not None:
        return rule.session_commands(task, session)
    if rule.opted_in_commands is not None and (
        operator_opt_in or row_carries_human_opt_in(task)
    ):
        return rule.opted_in_commands(task)
    if rule.no_finalize_commands is not None and not finalize_in_pipeline:
        return rule.no_finalize_commands(task)
    commands = rule.commands(task)
    if (
        task.status == QueueItemStatus.PENDING
        and skip_reason in _LANE_RESUME_SKIP_CODES
        and lane_resume_command is not None
    ):
        resume = NextCommand(command=lane_resume_command, purpose=LANE_RESUME_PURPOSE)
        return [resume, *commands]
    return commands


def _skipped(command: str, skip: str | None) -> bool:
    """True iff *skip* is *command*'s verb token or one of its flag tokens."""
    if skip is None:
        return False
    tokens = shlex.split(command)
    return tokens[2] == skip or (
        skip.startswith("-") and skip in [t for t in tokens[3:] if t.startswith("-")]
    )


def _verb_token(command: str) -> list[str]:
    """*command*'s verb token (``cw dev-queue <verb> ...``), as a 0/1-item list."""
    return shlex.split(command)[2:3]


def is_human_gate_command(task: TicketTask, command: NextCommand) -> bool:
    """True iff *command* is a human-kept gate's command an agent must not run.

    State-derived (R15): *task* is at a human-kept gate and the command's verb
    releases (``approve`` / ``requeue``); the ``operator_only`` flag is only
    an extra trigger, so a builder that forgets it cannot fail open.
    """
    if command.operator_only:
        return True
    return is_human_kept_gate(task) and any(
        verb in _RELEASE_VERBS for verb in _verb_token(command.command)
    )


def human_gate_attention_command(task: TicketTask) -> str:
    """What a purpose-dropping channel prints instead of a human gate's command."""
    return f"{HUMAN_GATE_CELL_LABEL}{dev_queue_command('show', task)}"


def _human_gate_refusal(command: str, position: PipelinePosition) -> str:
    rerun = _verb_token(command) == ["requeue"] and not position.is_terminal
    noun = "re-run" if rerun else "release"
    return (
        "This is a human-kept gate the operator opted into: an agent must not"
        " approve, release or requeue it. Notify the human operator. The"
        f" operator's {noun} command is `{command}` (operator-only; not for an"
        " agent to run)."
    )


def refusal_suffix(
    task: TicketTask,
    *,
    session: Session | None,
    skip: str | None = None,
    pipeline_position: PipelinePosition = PipelinePosition.INTERIOR,
    finalize_in_pipeline: bool = True,
) -> str:
    """The text a refusal appends: the row's state and its legal next commands.

    *skip* drops every command whose verb token or a flag token equals it (a
    refusal never re-suggests the verb that just refused); if nothing is left
    the row's ``show`` is named instead. A human-kept gate's command gets the
    operator-only form, which never says ``Run:``; it names the operator's
    command (``approve``, or the labelled ``requeue``) as operator-only, never
    as an agent step. An off-pipeline row leads with ``show`` and falls back
    to the ordinary form. *pipeline_position* and *finalize_in_pipeline* are
    forwarded to :func:`next_commands`. Uses the row-level opt-in check only
    (``operator_opt_in`` stays False).
    """
    commands = [
        c
        for c in next_commands(
            task,
            session=session,
            pipeline_position=pipeline_position,
            finalize_in_pipeline=finalize_in_pipeline,
        )
        if not _skipped(c.command, skip)
    ]
    head = f"This row is {_row_state(task)}."
    if commands and is_human_gate_command(task, commands[0]):
        refusal = _human_gate_refusal(commands[0].command, pipeline_position)
        return f" {head} {refusal}"
    if not commands:
        commands = [
            NextCommand(command=dev_queue_command("show", task), purpose=_SHOW_PURPOSE)
        ]
    parts = [head]
    guidance = PARK_GUIDANCE.get(task.disposition or "")
    if guidance is not None:
        parts.append(guidance)
    first, *rest = commands
    parts.append(f"Run: `{first.command}` ({first.purpose}).")
    parts.extend(f"Alternatively: `{c.command}` ({c.purpose})." for c in rest)
    return " " + " ".join(parts)


def compact_command(task: TicketTask, command: str) -> str:
    """*command* without the ``cw dev-queue`` prefix and the row's own tokens."""
    row_tokens = f" {shlex.quote(task.ticket_id)} -c {shlex.quote(task.client)}"
    return command.removeprefix("cw dev-queue ").replace(row_tokens, "", 1)
