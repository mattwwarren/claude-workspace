"""The human-gate and finalize-park rules (0a-0e) and the BLOCKED_ON_USER
rules (5-11): scope drift, must-fix (overridden, held and no-finalize
variants), attempt cap, salvage, drain, the approval gate and
sessionless rows.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    PLAN_SCOPE_DRIFT_BLOCKER_REASON,
    REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON,
)
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.dev_queue.dispositions import (
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_APPROVAL_GATE_DISPOSITION,
)
from cw.dev_queue.finalize_park import (
    FINALIZE_APPROVAL_PARK_PURPOSE,
    REVIEW_LESS_APPROVE_PURPOSE,
    REVIEW_LESS_APPROVED_APPROVE_PURPOSE,
    REVIEW_LESS_APPROVED_REQUEUE_PURPOSE,
    REVIEW_LESS_REQUEUE_PURPOSE,
    STALE_TIP_FETCH_PURPOSE,
    approval_recorded_after_park,
    stale_tip_park_reason,
    stale_tip_purpose,
)
from cw.dev_queue.gates import (
    DRAIN_DISPOSITIONS,
    _not_at_approval_gate,
)
from cw.dev_queue.next_commands._common import (
    _NO_GATE_SESSION,
    _SHIP_STEP_IS_HUMANS,
    _TERMINAL_REFUSES,
    HUMAN_GATE_PURPOSE_LABEL,
    SCOPE_DRIFT_PATHS_PLACEHOLDER,
    _approve,
    _human_approve,
    _human_gate_name,
    _human_requeue,
    _is_blocked,
    _override_must_fix,
    _requeue,
    _requeue_at_stage,
    _rerun,
    _ship_under_override,
    _stage_flag,
    dev_queue_command,
    is_human_kept_gate,
)
from cw.models import DispatchSkipReason, QueueItemStatus, ReapReason, Stage
from cw.models.ticket_view import NextCommand
from cw.review_convergence import TIP_CHECK_FETCH_FAILED

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


# --- predicates: (task, session) -> bool --------------------------------------


def _review_approval_gate_parked(task: TicketTask, _session: Session | None) -> bool:
    """A claim-time review-approval park (#2773): FINALIZE, no bound session.

    Rule 0c needs the bound session's sentinel; this park ran no session.
    """
    return (
        _is_blocked(task)
        and task.stage == Stage.FINALIZE
        and task.disposition == REVIEW_APPROVAL_GATE_DISPOSITION
    )


def _human_gate_off_gate(task: TicketTask, session: Session | None) -> bool:
    """A human-kept BLOCKED row ``approve`` cannot take: no bound session at the gate.

    ``approve`` refuses it at "session not found" or "not at an approval gate"
    (the shared ``gates._not_at_approval_gate``, as rule 9's predicate uses).
    """
    return (
        is_human_kept_gate(task)
        and _is_blocked(task)
        and (session is None or _not_at_approval_gate(session, task))
    )


def _human_gate(task: TicketTask, _session: Session | None) -> bool:
    return is_human_kept_gate(task)


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


def _confirm_must_fix(task: TicketTask, _session: Session | None) -> bool:
    """The confirm round after a repo review engine pass parked the row (#3042)."""
    return (
        _is_blocked(task)
        and task.blocked_reason == REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON
    )


def _confirm_must_fix_overridden(task: TicketTask, session: Session | None) -> bool:
    return _confirm_must_fix(task, session) and task.must_fix_override is not None


def _attempt_capped(task: TicketTask, _session: Session | None) -> bool:
    return (
        _is_blocked(task)
        and task.disposition == DispatchSkipReason.ATTEMPT_CAP_BLOCKED.value
    )


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
    return [_approve(task, _human_gate_purpose(task), operator_only=True)]


def _human_release_and_ship(task: TicketTask, reason: str) -> list[NextCommand]:
    """At the terminal stage a re-run re-evaluates no gate, so it ships (R14)."""
    body = f"release the {_human_gate_name(task)} gate and ship; {reason}"
    return [_human_requeue(task, body, *_stage_flag(task))]


def _human_gate_terminal_commands(task: TicketTask) -> list[NextCommand]:
    return _human_release_and_ship(task, _TERMINAL_REFUSES)


def _finalize_approval_park_commands(task: TicketTask) -> list[NextCommand]:
    """The operator's ``requeue --stage review`` (R18): approval lives at REVIEW."""
    return [
        _human_requeue(
            task, FINALIZE_APPROVAL_PARK_PURPOSE, "--stage", Stage.REVIEW.value
        )
    ]


def _finalize_approval_park_review_less_commands(
    task: TicketTask, session: Session | None
) -> list[NextCommand]:
    """Rule 0c on a lane with no review stage (#2779): approve, then ship.

    ``requeue --stage review`` is refused there, so the operator's ``approve``
    binds the approval to the branch head and ``requeue --stage finalize``
    ships. The requeue leads only once an approval was recorded after a
    ``missing`` park; after a ``stale`` park the approval is not valid for the
    head, so approve stays first (a requeue-first order would park again).
    """
    finalize = ("--stage", Stage.FINALIZE.value)
    if approval_recorded_after_park(task, session):
        return [
            _human_requeue(task, REVIEW_LESS_APPROVED_REQUEUE_PURPOSE, *finalize),
            _human_approve(task, REVIEW_LESS_APPROVED_APPROVE_PURPOSE),
        ]
    return [
        _human_approve(task, REVIEW_LESS_APPROVE_PURPOSE),
        _human_requeue(task, REVIEW_LESS_REQUEUE_PURPOSE, *finalize),
    ]


def _finalize_stale_tip_park_commands(
    task: TicketTask, session: Session | None
) -> list[NextCommand]:
    """``requeue --stage review`` (#2911): agent-runnable unless human-kept.

    The requeue cannot ship an unreviewed commit: review re-runs on the
    current head, and finalize re-checks the tip before every auto-merge arm.
    A ``tip_check_fetch_failed`` park (#2910) is not a review problem: it lists
    the plain ``requeue --stage <current stage>`` instead, which re-runs
    finalize and its tip check once the fetch works.
    """
    reason = stale_tip_park_reason(task, session) or ""
    if reason == TIP_CHECK_FETCH_FAILED:
        rerun = _stage_flag(task)
        if is_human_kept_gate(task):
            return [_human_requeue(task, STALE_TIP_FETCH_PURPOSE, *rerun)]
        return [_requeue(task, STALE_TIP_FETCH_PURPOSE, *rerun)]
    purpose = stale_tip_purpose(task, reason)
    review = ("--stage", Stage.REVIEW.value)
    if is_human_kept_gate(task):
        return [_human_requeue(task, purpose, *review)]
    return [_requeue(task, purpose, *review)]


def _human_gate_off_gate_commands(task: TicketTask) -> list[NextCommand]:
    body = f"re-run the {task.stage.value} stage; {_NO_GATE_SESSION}"
    return [_human_requeue(task, body, *_stage_flag(task))]


def _human_gate_off_gate_terminal_commands(task: TicketTask) -> list[NextCommand]:
    return _human_release_and_ship(task, _NO_GATE_SESSION)


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


def _must_fix_no_finalize_commands(task: TicketTask) -> list[NextCommand]:
    """Rules 6/6a in a lane with no finalize stage (#2765): a plain re-run.

    There is no ship step to list and no override to record: only finalize
    honors a MUST_FIX override, and a same-stage REVIEW requeue clears it.
    """
    return [
        _requeue(
            task,
            f"re-run the {task.stage.value} stage; finalize is not in this"
            " lane's pipeline, so there is no ship step and no MUST_FIX"
            " override can be honored",
        )
    ]


def _must_fix_held_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(
            task,
            f"record an operator override of the MUST_FIX park; {_SHIP_STEP_IS_HUMANS}",
        )
    ]


def _confirm_regress(task: TicketTask) -> NextCommand:
    """Rule 6c's last step: a real defect goes back to implementation (#3042)."""
    return _requeue(
        task,
        "send a real defect (or a never-deferrable coverage/acceptance"
        " finding) back to impl",
        "--stage",
        Stage.IMPL.value,
        "--regress",
    )


def _confirm_must_fix_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(
            task, "record an operator override of the engine-confirm MUST_FIX park"
        ),
        _ship_under_override(task, "then ship it under that override"),
        _confirm_regress(task),
    ]


def _confirm_must_fix_held_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _override_must_fix(
            task,
            "record an operator override of the engine-confirm MUST_FIX park;"
            f" {_SHIP_STEP_IS_HUMANS}",
        ),
        _confirm_regress(task),
    ]


def _confirm_must_fix_overridden_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _ship_under_override(task, "ship it under the recorded MUST_FIX override"),
        _override_must_fix(task, "optional: re-record the override first"),
        _confirm_regress(task),
    ]


def _confirm_must_fix_overridden_held_commands(
    task: TicketTask,
) -> list[NextCommand]:
    return [
        _override_must_fix(
            task,
            "re-record the MUST_FIX override if the verdict changed;"
            f" {_SHIP_STEP_IS_HUMANS}",
        ),
        _confirm_regress(task),
    ]


def _attempt_capped_commands(task: TicketTask) -> list[NextCommand]:
    return [
        _requeue(
            task,
            "re-run the stage with the unproductive-attempt counter reset"
            " (lane and priority kept)",
            "--reset-attempts",
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


def _gate_terminal_commands(task: TicketTask) -> list[NextCommand]:
    # Not human-kept by construction, and a same-stage re-run of the last
    # stage jumps no gate (REVIEW is behind it), so the opt-in is not consulted.
    purpose = f"re-run the {task.stage.value} stage; {_TERMINAL_REFUSES}"
    return [_requeue_at_stage(task, purpose)]


def _sessionless_commands(task: TicketTask) -> list[NextCommand]:
    return [_requeue(task, "session not found; approve refuses, re-run the stage")]
