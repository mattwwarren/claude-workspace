"""The legal next ``cw`` commands for a dev-queue row as it stands (#2691, #2692).

One ordered rule table, first match wins, mapping a row's state to the
commands an operator (or agent) can run next. ``cw dev-queue show`` renders
it, ``cw dev-queue tasks`` shows its first command in the NEXT column, and
every dev-queue refusal appends it through :func:`refusal_suffix` rather than
re-deriving which verb fits which park.

Pure: no state is loaded and nothing is resolved. The caller passes the
``Session`` the row is bound to -- ``show`` resolves it with
``cw.reconcile._shared.resolve_session_for_task`` and the refusal sites with
``cw.dev_queue.refusal.bound_session`` (both id-only, never raising).
``approve`` itself resolves with ``find_by_name_or_id``, which is id-first, so
the two agree for any row whose ``session_id`` is a ``Session.id``; a legacy
row holding a session *name* resolves to no session here and falls toward
``requeue``, never toward an ``approve`` the verb would refuse. A human-kept
BLOCKED row bound that way gets rule 0a's conservative "approve will refuse"
clause although ``approve`` would find the session by name; the command
listed is still ``approve`` and no ``requeue`` is ever emitted for it.

Human-kept gates (an operator-signoff park, a finalize hold, a
``scope_hint: large`` approval gate) list only the operator's ``approve``,
labelled operator-only: cw never recommends that an agent release a gate the
human opted into. For the same reason rules 6/6a drop the ``requeue --stage
finalize`` jump past REVIEW for a row that carries a human opt-in. The row
check is partial (lane and global opt-ins are not on the row); ``show`` and
``tasks`` pass the config-resolved opt-in as ``operator_opt_in``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, NamedTuple

from cw.auto_dev_result import (
    PLAN_SCOPE_DRIFT_BLOCKER_REASON,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_LARGE,
)
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.dev_queue.gates import (
    AMBIGUITIES_PENDING_RESOLUTION_STATUS,
    APPROVAL_GATE_REASON,
    DRAIN_DISPOSITIONS,
    MERGE_GATE_BLOCKED_STATUS,
    PREMISES_PENDING_VERIFICATION_STATUS,
    _not_at_approval_gate,
)
from cw.dev_queue.lifecycle import (
    AWAITING_OPERATOR_DISPOSITION,
    BRANCH_STALENESS_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
)
from cw.models import QueueItemStatus, ReapReason, Stage
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import Session, TicketTask

# Operator-supplied values: the only places a command is not runnable as-is.
SCOPE_DRIFT_PATHS_PLACEHOLDER = "<paths>"
_REASON_PLACEHOLDER = '"<why>"'

HUMAN_GATE_LABEL = "operator-only human gate; not for an agent to run"
HUMAN_GATE_PURPOSE_LABEL = f"{HUMAN_GATE_LABEL}: "
HUMAN_GATE_CELL_LABEL = "HUMAN GATE: "
_SESSIONLESS_HUMAN_GATE_CLAUSE = (
    "; the bound session is no longer recorded, so approve will refuse until"
    " the operator resolves that"
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
# stamps the verbatim sentinel status (lifecycle._derive_disposition).
_SCOPE_HINT_GATE_DISPOSITIONS: frozenset[str] = SCOPE_GATED_APPROVAL_STATUSES | {
    APPROVAL_GATE_REASON
}

_FROM_FLAGS: dict[QueueItemStatus, str] = {
    QueueItemStatus.COMPLETED: "--from-completed",
    QueueItemStatus.FAILED: "--from-failed",
    QueueItemStatus.CANCELLED: "--from-cancelled",
}

# Why a parked row with this disposition sits where it does, prepended to the
# refusal's commands. Dispositions without an entry fall through to the rule
# commands alone.
_PARK_GUIDANCE: dict[str, str] = {
    AMBIGUITIES_PENDING_RESOLUTION_STATUS: (
        "The worker parked on open ambiguities. Answer them on the ticket, then"
        " re-run the stage."
    ),
    PREMISES_PENDING_VERIFICATION_STATUS: (
        "The worker parked on unverified premises. Verify them and record the"
        " results on the ticket, then re-run the stage."
    ),
    REVIEW_HEALTH_GATE_DISPOSITION: (
        "Review health is degraded. This clears by re-running review, not by approving."
    ),
    REVIEW_STALENESS_GATE_DISPOSITION: (
        "The review artifacts predate the branch head. Re-running review clears"
        " it; approving does not."
    ),
    BRANCH_STALENESS_GATE_DISPOSITION: (
        "The branch is behind the default branch. Rebase it, then re-run the"
        " stage; approving would ship a tree that no longer matches main."
    ),
    MERGE_GATE_BLOCKED_STATUS: (
        "A prior pipeline PR for this ticket is still open. Merge or close it,"
        " then re-run the stage."
    ),
    STALE_DISPATCH_DISPOSITION: (
        "An earlier dispatch left an open, unmerged PR for this ticket. Land or"
        " close it, then re-run the stage."
    ),
    AWAITING_OPERATOR_DISPOSITION: (
        "The park was an operator-unavailability hold. Re-running the stage, or"
        " draining held tickets, releases it."
    ),
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


def _requeue(task: TicketTask, purpose: str, *extra: str) -> NextCommand:
    return NextCommand(
        command=dev_queue_command("requeue", task, *extra), purpose=purpose
    )


def _requeue_at_stage(task: TicketTask) -> NextCommand:
    """``requeue --stage <current stage>``; HARDEN is not a ``--stage`` choice."""
    purpose = f"re-run the {task.stage.value} stage instead"
    if task.stage is Stage.HARDEN:
        return _requeue(task, purpose)
    return _requeue(task, purpose, "--stage", task.stage.value)


def _rerun(task: TicketTask) -> NextCommand:
    return _requeue(task, f"re-run the {task.stage.value} stage")


def _approve(task: TicketTask, purpose: str, *extra: str) -> NextCommand:
    return NextCommand(
        command=dev_queue_command("approve", task, *extra), purpose=purpose
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


# --- predicates: (task, session) -> bool --------------------------------------


def _is_blocked(task: TicketTask) -> bool:
    return task.status == QueueItemStatus.BLOCKED_ON_USER


def _human_gate_sessionless(task: TicketTask, session: Session | None) -> bool:
    return is_human_kept_gate(task) and _is_blocked(task) and session is None


def _human_gate(task: TicketTask, _session: Session | None) -> bool:
    return is_human_kept_gate(task)


def _pending(task: TicketTask, _session: Session | None) -> bool:
    return task.status == QueueItemStatus.PENDING


def _running(task: TicketTask, _session: Session | None) -> bool:
    return task.status == QueueItemStatus.RUNNING


def _terminal(task: TicketTask, _session: Session | None) -> bool:
    return task.status in _FROM_FLAGS


def _scope_drift(task: TicketTask, _session: Session | None) -> bool:
    return (
        _is_blocked(task)
        and task.blocked_reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON
        and task.stage == Stage.IMPL
    )


def _must_fix(task: TicketTask, _session: Session | None) -> bool:
    return _is_blocked(task) and task.blocked_reason == CODEX_MUST_FIX_FINDINGS


def _must_fix_overridden(task: TicketTask, session: Session | None) -> bool:
    return _must_fix(task, session) and task.must_fix_override is not None


def _salvage_parked(task: TicketTask, session: Session | None) -> bool:
    return (
        _is_blocked(task)
        and session is not None
        and session.reap_reason == ReapReason.SALVAGE_PARKED
    )


def _drain_held(task: TicketTask, _session: Session | None) -> bool:
    return _is_blocked(task) and task.disposition in DRAIN_DISPOSITIONS


def _at_approval_gate(task: TicketTask, session: Session | None) -> bool:
    return (
        _is_blocked(task)
        and session is not None
        and not _not_at_approval_gate(session, task)
    )


def _sessionless(task: TicketTask, session: Session | None) -> bool:
    return _is_blocked(task) and session is None


def _anything(_task: TicketTask, _session: Session | None) -> bool:
    return True


# --- builders: (task) -> commands, in the order they should be tried ---------


def _human_gate_purpose(task: TicketTask) -> str:
    if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        body = "clear the operator-signoff gate so the ticket ships"
    elif task.disposition == FINALIZE_GATE_HELD_DISPOSITION:
        body = "release the operator-set finalize hold so the ticket finalizes"
    else:
        body = (
            f"release the {task.stage.value} approval gate the operator set with"
            " scope_hint: large"
        )
    return f"{HUMAN_GATE_PURPOSE_LABEL}{body}"


def _human_gate_commands(task: TicketTask) -> list[NextCommand]:
    return [_approve(task, _human_gate_purpose(task))]


def _human_gate_sessionless_commands(task: TicketTask) -> list[NextCommand]:
    purpose = f"{_human_gate_purpose(task)}{_SESSIONLESS_HUMAN_GATE_CLAUSE}"
    return [_approve(task, purpose)]


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


def _terminal_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _requeue(
            task,
            f"recover the {task.status.value} row to PENDING at its"
            f" {task.stage.value} stage",
            _FROM_FLAGS[task.status],
        )
    ]


def _scope_drift_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _approve(
            task,
            "allow the extra repo-relative files (comma-separated) and re-run impl",
            "--scope-drift",
            SCOPE_DRIFT_PATHS_PLACEHOLDER,
        ),
        _requeue(task, "re-run impl without granting the extra files"),
    ]


def _must_fix_overridden_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _ship_under_override(task, "ship it under the recorded MUST_FIX override"),
        _override_must_fix(task, "optional: re-record the override first"),
    ]


def _must_fix_overridden_held_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(
            task,
            "re-record the MUST_FIX override if the verdict changed;"
            f" {_SHIP_STEP_IS_HUMANS}",
        )
    ]


def _must_fix_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(task, "record an operator override of the MUST_FIX park"),
        _ship_under_override(task, "then ship it under that override"),
    ]


def _must_fix_held_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(
            task,
            f"record an operator override of the MUST_FIX park; {_SHIP_STEP_IS_HUMANS}",
        )
    ]


def _salvage_commands(task: TicketTask) -> list[NextCommand]:
    return [
        NextCommand(
            command=dev_queue_command("unblock", task),
            purpose="clear the salvage park and requeue the ticket",
        ),
        _rerun(task),
    ]


def _drain_commands(task: TicketTask) -> list[NextCommand]:
    client = shlex.quote(task.client)
    return [
        _requeue(task, "release this ticket only"),
        NextCommand(
            command=f"cw dev-queue drain --held -c {client}",
            purpose=(
                f"releases ALL held tickets of client {task.client}, not just"
                " this one; add --dry-run to preview"
            ),
        ),
    ]


def _gate_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _approve(task, f"release the {task.stage.value} approval gate"),
        _requeue_at_stage(task),
    ]


def _sessionless_commands(task: TicketTask) -> list[NextCommand]:
    return [_requeue(task, "session not found; approve refuses, re-run the stage")]


def _generic_commands(task: TicketTask) -> list[NextCommand]:
    return [_rerun(task)]


class _Rule(NamedTuple):
    matches: Callable[[TicketTask, Session | None], bool]
    commands: Callable[[TicketTask], list[NextCommand]]
    # Used instead of ``commands`` when the row carries a human opt-in (R5).
    opted_in_commands: Callable[[TicketTask], list[NextCommand]] | None = None


# First match wins. Rules 0a/0b run first and cover every human-kept gate;
# rules 5-11 are BLOCKED_ON_USER only; 2-4 cover the other statuses. Scope-drift
# and must-fix approvals never consult the session, so only the plain-approve
# rules (9/10) and 0a depend on it. Rule 8 runs before 9 so an availability
# hold is released by requeue/drain, and DRAIN_DISPOSITIONS deliberately
# excludes branch_behind_main (#1823), which falls to the generic requeue.
_RULES: tuple[_Rule, ...] = (
    _Rule(_human_gate_sessionless, _human_gate_sessionless_commands),  # 0a
    _Rule(_human_gate, _human_gate_commands),  # 0b
    _Rule(_pending, _pending_commands),  # 2
    _Rule(_running, _running_commands),  # 3
    _Rule(_terminal, _terminal_commands),  # 4
    _Rule(_scope_drift, _scope_drift_commands),  # 5
    _Rule(
        _must_fix_overridden,
        _must_fix_overridden_commands,
        _must_fix_overridden_held_commands,
    ),  # 6a
    _Rule(_must_fix, _must_fix_commands, _must_fix_held_commands),  # 6
    _Rule(_salvage_parked, _salvage_commands),  # 7
    _Rule(_drain_held, _drain_commands),  # 8
    _Rule(_at_approval_gate, _gate_commands),  # 9
    _Rule(_sessionless, _sessionless_commands),  # 10
    _Rule(_anything, _generic_commands),  # 11
)


def next_commands(
    task: TicketTask, *, session: Session | None, operator_opt_in: bool = False
) -> list[NextCommand]:
    """The legal next commands for *task*, most useful first.

    *session* is the cw ``Session`` *task* is bound to, or None when it has
    none or it is gone; see the module docstring for how to resolve it.
    *operator_opt_in* is the config-resolved human opt-in (lane or global
    finalize hold / signoff) the row cannot show; it only affects rules 6/6a.
    """
    rule = next(r for r in _RULES if r.matches(task, session))
    if rule.opted_in_commands is not None and (
        operator_opt_in or row_carries_human_opt_in(task)
    ):
        return rule.opted_in_commands(task)
    return rule.commands(task)


def _skipped(command: str, skip: str | None) -> bool:
    """True iff *skip* is *command*'s verb token or one of its flag tokens."""
    if skip is None:
        return False
    tokens = shlex.split(command)
    return tokens[2] == skip or (
        skip.startswith("-") and skip in [t for t in tokens[3:] if t.startswith("-")]
    )


def _human_gate_refusal(task: TicketTask, command: str, session: Session | None) -> str:
    text = (
        "This is a human-kept gate the operator opted into: an agent must not"
        " approve, release or requeue it. Notify the human operator. The"
        f" operator's release command is `{command}` (operator-only; not for an"
        " agent to run)."
    )
    if _is_blocked(task) and session is None:
        text += (
            " The bound session is no longer recorded, so approve cannot bind this"
            " release; the human operator must resolve it."
        )
    return text


def refusal_suffix(
    task: TicketTask, *, session: Session | None, skip: str | None = None
) -> str:
    """The text a refusal appends: the row's state and its legal next commands.

    *skip* drops every command whose verb token or a flag token equals it (a
    refusal never re-suggests the verb that just refused); if nothing is left
    the row's ``show`` is named instead. A human-kept gate gets the
    operator-only form, which never says ``Run:`` and never names a requeue.
    Uses the row-level opt-in check only (``operator_opt_in`` stays False).
    """
    commands = [
        c for c in next_commands(task, session=session) if not _skipped(c.command, skip)
    ]
    head = f"This row is {_row_state(task)}."
    if commands and is_human_kept_gate(task):
        return f" {head} {_human_gate_refusal(task, commands[0].command, session)}"
    if not commands:
        commands = [
            NextCommand(command=dev_queue_command("show", task), purpose=_SHOW_PURPOSE)
        ]
    parts = [head]
    guidance = _PARK_GUIDANCE.get(task.disposition or "")
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
