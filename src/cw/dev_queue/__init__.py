"""Dev-queue management for orchestrator ticket dispatch.

Package split (#1317 part 1, #1318 part 2). The historical flat ``cw.dev_queue``
module is being converted into a package of focused submodules:

* ``migrate`` — the pure dict-in / dict-out schema-normalisation layer.
* ``storage`` — the on-disk persistence layer (file locks + load/save).
* ``lifecycle`` — task status transitions, stage-pointer helpers, and the
  terminal-wait poll loop (#1318 part 2).
* ``crud`` — operator-facing queue mutations and the ticket-resolution helpers
  (#1318 part 2).
* ``approval`` — the plan/review approval + operator-signoff-clearing gates
  (#1318 part 2).

The remaining ``requeue`` concern still lives in this ``__init__`` and moves out
in the final part-2 commit.

This ``__init__`` re-exports the full historical public + private surface (see
``__all__``) so every ``from cw.dev_queue import X`` import site keeps working
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import get_client
from cw.dev_queue.approval import _approve_ticket_locked, approve_ticket
from cw.dev_queue.crud import (
    _APPROVABLE_STATUSES,
    _find_ticket,
    _newest_by_created_at,
    add_ticket,
    cancel_task_for_session,
    cancel_ticket,
    clear_tickets,
    list_tickets,
    move_ticket,
    register_watched_pr,
    remove_ticket,
    resolve_client,
)
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    SIGNOFF_GATE_DISPOSITION,
    _advance_task_pointer,
    _derive_disposition,
    _emit_stage_change,
    _extract_pr_url,
    _raise_stage_high_water,
    _reset_for_same_stage_requeue,
    _stage_regress,
    consume_completed_sessions,
    transition_task_status,
    wait_for_terminal,
)
from cw.dev_queue.migrate import migrate_dev_queue
from cw.dev_queue.storage import (
    _lock,
    dev_queue_lock,
    load_dev_queue,
    load_plan,
    plan_path,
    save_dev_queue,
    save_plan,
)
from cw.exceptions import (
    LaneNotFoundError,
    RequeueStageError,
    RequeueStateError,
    UnblockStateError,
)
from cw.models import QueueItemStatus, Stage

if TYPE_CHECKING:
    from cw.models import TicketTask

# Issue #917: --regress requires an explicit backward --stage target.
_REGRESS_NEEDS_BACKWARD_STAGE_MSG = (
    "--regress requires a backward --stage target on a blocked task."
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


def _apply_requeue_stage(
    task: TicketTask,
    stages: list[Stage],
    stage_override: str | None,
    *,
    allow_regress: bool,
) -> bool:
    """Resolve a requeue stage_override and mutate ``task``; report regress.

    Returns True iff the task was regressed backward (via ``_stage_regress``);
    False for forward/same-stage moves (task.stage is set forward, but status
    transition + session resets are left to the caller).

    Backward moves are only permitted when ``allow_regress`` is set AND the task
    is BLOCKED_ON_USER; otherwise a RequeueStageError is raised. This keeps the
    backward-refusal exception type distinct from the forward-path
    RequeueStateError (see #917).

    ``client_name``/``ticket_id`` are read off ``task`` rather than taken as
    separate params: callers always source ``task`` via ``_find_ticket``,
    which guarantees ``task.client``/``task.ticket_id`` already match.
    """
    if stage_override is None:
        return False

    target_stage = Stage(stage_override)
    if target_stage not in stages:
        msg = (
            f"Stage '{stage_override}' is not in the pipeline"
            f" for client '{task.client}'."
        )
        raise RequeueStageError(msg)

    current_idx = stages.index(task.stage)
    target_idx = stages.index(target_stage)

    if target_idx < current_idx:
        if not allow_regress:
            msg = (
                f"Cannot requeue ticket '{task.ticket_id}'"
                f" to stage '{stage_override}':"
                f" that would regress from '{task.stage.value}'."
                " Only same-stage or forward advancement is allowed."
                " Use --regress to move backward."
            )
            raise RequeueStageError(msg)
        if task.status not in _APPROVABLE_STATUSES:
            msg = (
                f"Cannot regress ticket '{task.ticket_id}'"
                f" to stage '{stage_override}': status is"
                f" {task.status.value!r}, expected BLOCKED_ON_USER or"
                " AWAITING_OPERATOR_SIGNOFF."
            )
            raise RequeueStageError(msg)
        _stage_regress(task, target_stage)
        return True

    # Forward or same-stage: caller enforces the BLOCKED_ON_USER precondition.
    old_stage = task.stage
    task.stage = target_stage
    _raise_stage_high_water(task, stages, target_stage)
    # Forward stage move → direction="advance"; the same-stage case is naturally
    # guarded silent by _emit_stage_change's old==new check. RFC 0008 W1.
    _emit_stage_change(task, old_stage, target_stage, "advance")
    return False


def _requeue_state_error_message(ticket_id: str, status: QueueItemStatus) -> str:
    """Build the RequeueStateError message for the forward/same-stage gate.

    Names the --from-cancelled or --from-failed escape hatch only when it
    would actually apply (status is CANCELLED or FAILED, respectively) so
    the message doesn't mislead callers hitting the gate from PENDING/
    RUNNING/etc. See GitHub #1018, #1190.
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
    return base + "."


def requeue_ticket(
    ticket_id: str,
    client_name: str,
    stage_override: str | None = None,
    *,
    allow_regress: bool = False,
    from_cancelled: bool = False,
    from_failed: bool = False,
) -> dict[str, str | bool | int]:
    """Requeue a BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF ticket, optionally
    at a specific stage.

    Returns dict with from_stage, to_stage, ticket_id, client, regressed,
    regress_attempts, from_cancelled_applied, and from_failed_applied for
    event emission. ``regressed`` is True only on a genuine backward regress
    (``allow_regress`` + backward ``stage_override`` + blocked task);
    ``regress_attempts`` is the post-mutation attempt count (0 on the
    forward/same-stage path).
    ``from_cancelled_applied`` is True only when the CANCELLED-acceptance
    branch actually admitted the row — i.e. ``from_cancelled=True`` AND the
    row's status was CANCELLED — not merely when the caller passed
    ``from_cancelled=True``. ``from_failed_applied`` is the analogous field
    for the FAILED-acceptance branch. Callers must key any "recovered from
    CANCELLED"/"recovered from FAILED" provenance signal (e.g. an event
    ``reason``) off these fields, not off the raw ``from_cancelled``/
    ``from_failed`` arguments, since either flag is harmlessly additive on
    an already-approvable row.

    Args:
        from_cancelled: when True, the forward/same-stage path also accepts a
            CANCELLED row (CLI: --from-cancelled). Recovers a ticket stranded
            by ``cw spawn close <sid> --confirmed-dead`` on a RUNNING row
            (#1018). Does NOT widen the backward-regress gate: a CANCELLED
            row combined with a backward stage_override still raises
            RequeueStageError, since _apply_requeue_stage's regress check is
            unchanged.
        from_failed: when True, the forward/same-stage path also accepts a
            FAILED row (CLI: --from-failed). Recovers a ticket whose row was
            marked FAILED even though the underlying session may have
            actually completed clean (#1190). Does NOT widen the
            backward-regress gate: a FAILED row combined with a backward
            stage_override still raises RequeueStageError, since
            _apply_requeue_stage's regress check is unchanged.

    Raises:
        RequeueStateError: if ticket is not BLOCKED_ON_USER or
            AWAITING_OPERATOR_SIGNOFF (forward path), unless from_cancelled
            is True and the ticket is CANCELLED, or from_failed is True and
            the ticket is FAILED.
        RequeueStageError: if stage_override would regress without allow_regress,
            is not in the client pipeline, regresses a non-blocked task, or if
            allow_regress is set with no backward stage_override.
        CwError: if no matching task is found.
    """
    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)

        client_cfg = get_client(client_name)
        stages = client_cfg.pipeline.stages

        from_stage = task.stage

        if allow_regress and stage_override is None:
            raise RequeueStageError(_REGRESS_NEEDS_BACKWARD_STAGE_MSG)

        regressed = _apply_requeue_stage(
            task,
            stages,
            stage_override,
            allow_regress=allow_regress,
        )

        from_cancelled_applied = False
        from_failed_applied = False
        if not regressed:
            # Forward/same-stage path (including inert allow_regress forward
            # targets) requires the ticket to be at a BLOCKED_ON_USER or
            # AWAITING_OPERATOR_SIGNOFF gate, OR (opt-in) CANCELLED/FAILED.
            approvable = task.status in _APPROVABLE_STATUSES
            cancelled_ok = (
                from_cancelled and task.status in _REQUEUE_FROM_CANCELLED_STATUSES
            )
            failed_ok = from_failed and task.status in _REQUEUE_FROM_FAILED_STATUSES
            if not (approvable or cancelled_ok or failed_ok):
                raise RequeueStateError(
                    _requeue_state_error_message(ticket_id, task.status)
                )
            from_cancelled_applied = cancelled_ok
            from_failed_applied = failed_ok
            _reset_for_same_stage_requeue(task)
            task.regress_attempts = 0

        to_stage = task.stage
        regress_attempts = task.regress_attempts if regressed else 0
        save_dev_queue(store)

    return {
        "from_stage": from_stage.value,
        "to_stage": to_stage.value,
        "ticket_id": ticket_id,
        "client": client_name,
        "regressed": regressed,
        "regress_attempts": regress_attempts,
        "from_cancelled_applied": from_cancelled_applied,
        "from_failed_applied": from_failed_applied,
    }


def unblock_ticket(ticket_id: str, client_name: str) -> dict[str, str]:
    """Clear salvage/park markers and requeue a SALVAGE_PARKED ticket.

    Returns dict with ticket_id, client for event emission.

    Raises:
        UnblockStateError: if ticket is not park-marked (reap_reason != SALVAGE_PARKED).
        CwError: if no matching task is found.
    """
    from cw.config import load_state, save_state, sessions_lock
    from cw.models import ReapReason

    # Fast-fail pre-check outside any lock; re-validated under sessions_lock below.
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)

    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot unblock ticket '{ticket_id}': status is {task.status.value!r},"
            " expected BLOCKED_ON_USER."
        )
        raise UnblockStateError(msg)

    session_id = task.session_id

    # Why: sessions_lock outer, dev_queue_lock inner — canonical dual-lock ordering.
    # Both saves happen under the outer lock so sessions.json is never written unless
    # dev_queue.json succeeds first (safe-fail direction for partial-commit scenarios).
    with sessions_lock():
        state = load_state()
        session = None
        if session_id is not None:
            session = state.find_by_name_or_id(session_id)

        if session is None or session.reap_reason != ReapReason.SALVAGE_PARKED:
            if session is None:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session not found"
                    f" (session_id={session_id!r}). The park-marked precondition"
                    " cannot be confirmed."
                )
            else:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session is not park-marked"
                    f" (reap_reason={session.reap_reason!r},"
                    " expected SALVAGE_PARKED)."
                )
            raise UnblockStateError(msg)

        with _lock():
            store = load_dev_queue()
            task = _find_ticket(store, ticket_id, client_name)
            if task.status != QueueItemStatus.BLOCKED_ON_USER:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': status changed to"
                    f" {task.status.value!r} concurrently. Re-check and retry."
                )
                raise UnblockStateError(msg)
            transition_task_status(task, QueueItemStatus.PENDING)
            task.session_id = None
            task.stage_base_ref = None
            save_dev_queue(store)

        session.last_result = None
        session.reap_reason = None
        save_state(state)

    return {"ticket_id": ticket_id, "client": client_name}


__all__ = [
    "SIGNOFF_GATE_DISPOSITION",
    "_PLAN_SOUNDNESS_MARKER",
    "_PLAN_SPEC_MARKER",
    "LaneNotFoundError",
    "_advance_task_pointer",
    "_apply_requeue_stage",
    "_approve_ticket_locked",
    "_derive_disposition",
    "_extract_pr_url",
    "_find_ticket",
    "_lock",
    "_newest_by_created_at",
    "_stage_regress",
    "add_ticket",
    "approve_ticket",
    "cancel_task_for_session",
    "cancel_ticket",
    "clear_tickets",
    "consume_completed_sessions",
    "dev_queue_lock",
    "list_tickets",
    "load_dev_queue",
    "load_plan",
    "migrate_dev_queue",
    "move_ticket",
    "plan_path",
    "register_watched_pr",
    "remove_ticket",
    "requeue_ticket",
    "resolve_client",
    "save_dev_queue",
    "save_plan",
    "transition_task_status",
    "unblock_ticket",
    "wait_for_terminal",
]
