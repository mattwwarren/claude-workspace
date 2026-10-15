"""The legal next ``cw`` commands for a dev-queue row as it stands (#2691).

One ordered rule table, first match wins, mapping a row's state to the
commands an operator (or agent) can run next. ``cw dev-queue show`` renders
it; #2692's refusal messages are meant to import it from here rather than
re-derive which verb fits which park.

Pure: no state is loaded and nothing is resolved. The caller passes the
``Session`` the row is bound to -- ``show`` resolves it with
``cw.reconcile._shared.resolve_session_for_task`` (id-only, never raises).
``approve`` itself resolves with ``find_by_name_or_id``, which is id-first, so
the two agree for any row whose ``session_id`` is a ``Session.id``; a legacy
row holding a session *name* resolves to no session here and falls toward
``requeue``, never toward an ``approve`` the verb would refuse.

Not mirrored, because they need the client's resolved pipeline or the daemon
roster: ``approve``'s terminal-stage and stage-not-in-pipeline refusals and
``requeue``'s live-session refusal. Those verbs refuse with their own
actionable messages.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, NamedTuple

from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.dev_queue.approval import _not_at_approval_gate
from cw.dev_queue.drain import DRAIN_DISPOSITIONS
from cw.models import QueueItemStatus, ReapReason, Stage
from cw.models.ticket_view import NextCommand

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import Session, TicketTask

# Operator-supplied values: the only places a command is not runnable as-is.
_SCOPE_DRIFT_PATHS_PLACEHOLDER = "<paths>"
_REASON_PLACEHOLDER = '"<why>"'

_FROM_FLAGS: dict[QueueItemStatus, str] = {
    QueueItemStatus.COMPLETED: "--from-completed",
    QueueItemStatus.FAILED: "--from-failed",
    QueueItemStatus.CANCELLED: "--from-cancelled",
}


def _dev_queue(verb: str, task: TicketTask, *extra: str) -> str:
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


def _requeue(task: TicketTask, purpose: str, *extra: str) -> NextCommand:
    return NextCommand(command=_dev_queue("requeue", task, *extra), purpose=purpose)


def _requeue_at_stage(task: TicketTask) -> NextCommand:
    """``requeue --stage <current stage>``; HARDEN is not a ``--stage`` choice."""
    purpose = f"re-run the {task.stage.value} stage instead"
    if task.stage is Stage.HARDEN:
        return _requeue(task, purpose)
    return _requeue(task, purpose, "--stage", task.stage.value)


def _rerun(task: TicketTask) -> NextCommand:
    return _requeue(task, f"re-run the {task.stage.value} stage")


def _approve(task: TicketTask, purpose: str, *extra: str) -> NextCommand:
    return NextCommand(command=_dev_queue("approve", task, *extra), purpose=purpose)


def _wait(task: TicketTask) -> NextCommand:
    return NextCommand(
        command=_dev_queue("wait", task),
        purpose="block until the ticket finishes or parks",
    )


def _override_must_fix(task: TicketTask, purpose: str) -> NextCommand:
    return _approve(
        task, purpose, "--override-must-fix", "--reason", _REASON_PLACEHOLDER
    )


def _ship_under_override(task: TicketTask, purpose: str) -> NextCommand:
    return _requeue(task, purpose, "--stage", Stage.FINALIZE.value)


# --- predicates: (task, session) -> bool --------------------------------------


def _is_blocked(task: TicketTask) -> bool:
    return task.status == QueueItemStatus.BLOCKED_ON_USER


def _at_signoff(task: TicketTask, _session: Session | None) -> bool:
    return task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF


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


def _signoff_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _approve(task, "clear the operator-signoff gate so the ticket ships"),
        _requeue(task, f"re-run the {task.stage.value} stage instead of shipping"),
    ]


def _pending_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _wait(task),
        NextCommand(
            command=_dev_queue("cancel", task), purpose="drop it from the queue"
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
            _SCOPE_DRIFT_PATHS_PLACEHOLDER,
        ),
        _requeue(task, "re-run impl without granting the extra files"),
    ]


def _must_fix_overridden_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _ship_under_override(task, "ship it under the recorded MUST_FIX override"),
        _override_must_fix(task, "optional: re-record the override first"),
    ]


def _must_fix_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(task, "record an operator override of the MUST_FIX park"),
        _ship_under_override(task, "then ship it under that override"),
    ]


def _salvage_commands(task: TicketTask) -> list[NextCommand]:
    return [
        NextCommand(
            command=_dev_queue("unblock", task),
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


# First match wins. Rules 5-11 are BLOCKED_ON_USER only; 1-4 cover the other
# six statuses. Signoff, scope-drift and must-fix approvals never consult the
# session, so only the plain-approve rules (9/10) depend on it. Rule 8 runs
# before 9 so an availability hold is released by requeue/drain, and
# DRAIN_DISPOSITIONS deliberately excludes branch_behind_main (#1823), which
# falls to the generic requeue.
_RULES: tuple[_Rule, ...] = (
    _Rule(_at_signoff, _signoff_commands),  # 1
    _Rule(_pending, _pending_commands),  # 2
    _Rule(_running, _running_commands),  # 3
    _Rule(_terminal, _terminal_commands),  # 4
    _Rule(_scope_drift, _scope_drift_commands),  # 5
    _Rule(_must_fix_overridden, _must_fix_overridden_commands),  # 6a
    _Rule(_must_fix, _must_fix_commands),  # 6
    _Rule(_salvage_parked, _salvage_commands),  # 7
    _Rule(_drain_held, _drain_commands),  # 8
    _Rule(_at_approval_gate, _gate_commands),  # 9
    _Rule(_sessionless, _sessionless_commands),  # 10
    _Rule(_anything, _generic_commands),  # 11
)


def next_commands(task: TicketTask, *, session: Session | None) -> list[NextCommand]:
    """The legal next commands for *task*, most useful first.

    *session* is the cw ``Session`` *task* is bound to, or None when it has
    none or it is gone; see the module docstring for how to resolve it.
    """
    rule = next(r for r in _RULES if r.matches(task, session))
    return rule.commands(task)
