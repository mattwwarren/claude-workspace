"""Dev-queue management for orchestrator ticket dispatch.

Package split (#1317 part 1, #1318 part 2). The historical flat ``cw.dev_queue``
module is being converted into a package of focused submodules:

* ``migrate`` — the pure dict-in / dict-out schema-normalisation layer.
* ``storage`` — the on-disk persistence layer (file locks + load/save).
* ``lifecycle`` — task status transitions, stage-pointer helpers, and the
  terminal-wait poll loop (#1318 part 2).
* ``crud`` — operator-facing queue mutations and the ticket-resolution helpers
  (#1318 part 2).

The remaining ``approval`` / ``requeue`` concerns still live in this ``__init__``
and move out in the rest of part 2.

This ``__init__`` re-exports the full historical public + private surface (see
``__all__``) so every ``from cw.dev_queue import X`` import site keeps working
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import get_client
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
    _clear_signoff_gate,
    _derive_disposition,
    _emit_stage_change,
    _extract_pr_url,
    _plan_is_reviewed,
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
    ApproveGateError,
    LaneNotFoundError,
    RequeueStageError,
    RequeueStateError,
    UnblockStateError,
)
from cw.models import QueueItemStatus, Stage

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask

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


def approve_ticket(ticket_id: str, client_name: str) -> dict[str, str | bool]:
    """Approve a plan/review approval gate, or clear an operator-signoff gate.

    Two distinct gates share this entry point (GitHub #990):
      - BLOCKED_ON_USER: the existing plan_pending_approval/review_pending_approval
        approval gate. Validates the owning session's last_result. When the
        ticket is at Stage.REVIEW and the resolved signoff policy requires an
        operator signoff, this approval re-routes the ticket to
        AWAITING_OPERATOR_SIGNOFF instead of advancing straight to FINALIZE --
        a second, explicit `approve` clears it.
      - AWAITING_OPERATOR_SIGNOFF: a ticket already parked for signoff (by
        dispatch's staged-decision routing, or by the re-route above). No
        session/last_result validation -- clears via ``_clear_signoff_gate``.

    Returns dict with from_stage, to_stage, ticket_id, client, awaiting_signoff
    (True iff *this* call parked the ticket at AWAITING_OPERATOR_SIGNOFF
    rather than advancing/completing it), and plan_requeued (True iff *this*
    call re-parked a PLAN-stage ticket at Stage.PLAN/PENDING instead of
    advancing to IMPL, because the plan-of-record was not yet quality-
    reviewed -- see GitHub #968; always present, False on every other path).

    Raises:
        ApproveGateError: if ticket is not at either gate, session is missing,
            last_result is absent, or last_result status is not an approval gate.
        CwError: if no matching task is found.
    """
    with _lock():
        return _approve_ticket_locked(ticket_id, client_name)


def _resolve_approval_target(
    store: DevQueueStore,
    ticket_id: str,
    client_name: str,
    resolved_task: TicketTask | None,
) -> TicketTask:
    """Select the physical row :func:`_approve_ticket_locked` acts on.

    ``resolved_task is None`` (public ``approve_ticket`` / CLI / API path): fall
    back to :func:`_find_ticket`'s status-pooled newest-wins resolution.

    ``resolved_task`` supplied (RFC 0009 gate-recipe path, #1083): the caller has
    ALREADY validated a specific physical row and must not have the mutation
    re-resolved to a different duplicate. Re-locate that exact row inside this
    freshly-loaded ``store`` by stable identity ``(ticket_id, client,
    created_at)`` -- ``created_at`` is set once at construction, never mutated by
    a transition, and round-trips through ``model_dump_json``/``model_validate``.
    Belt-and-suspenders: the matched row's status must equal the status the
    caller validated (not merely "any approvable status"), else the caller's
    premise no longer holds and we fail closed rather than clear a gate we never
    checked.

    Raises:
        ApproveGateError: if ``resolved_task`` is supplied but its identity is
            no longer present, or the matched row's status diverged from the
            validated status.
    """
    if resolved_task is None:
        return _find_ticket(store, ticket_id, client_name)
    for t in store.tasks:
        if (
            t.ticket_id == resolved_task.ticket_id
            and t.client == resolved_task.client
            and t.created_at == resolved_task.created_at
        ):
            if t.status != resolved_task.status:
                msg = (
                    f"Cannot approve ticket '{ticket_id}': resolved row status"
                    f" is {t.status.value!r}, expected"
                    f" {resolved_task.status.value!r} (the status the caller"
                    " validated)."
                )
                raise ApproveGateError(msg)
            return t
    msg = (
        f"Cannot approve ticket '{ticket_id}': the resolved row is no longer"
        " present in the dev queue."
    )
    raise ApproveGateError(msg)


def _approve_ticket_locked(
    ticket_id: str,
    client_name: str,
    *,
    resolved_task: TicketTask | None = None,
    plan_reviewed: bool | None = None,
) -> dict[str, str | bool]:
    """Lock-free body of :func:`approve_ticket`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Extracted
    from ``approve_ticket`` so an in-process caller that has *already* acquired
    the dev-queue lock — e.g. the RFC 0009 gate-recipe act phase
    (``cw.reconcile.gate_recipes``) — can invoke the approval mutation directly
    without a second acquisition of the same flock-based lock, which would
    self-deadlock (``_lock`` opens a fresh fd and blocks on ``LOCK_EX`` per
    call). All validation guards and return shape are identical to the public
    wrapper. See GitHub #1065.

    When ``resolved_task`` is supplied (RFC 0009 gate-recipe path, #1083) the
    mutation is pinned to the caller-validated physical row by stable identity
    rather than re-resolved via :func:`_find_ticket` -- see
    :func:`_resolve_approval_target`.

    ``plan_reviewed`` (GitHub #968) governs the PLAN-stage review-completeness
    gate: ``None`` (the public/CLI path's default) triggers a live
    :func:`_plan_is_reviewed` check; the trusted gate-recipe caller
    (``gate_recipes._act_auto_adopt_plan``) passes ``plan_reviewed=True``
    explicitly so this function never re-fetches the plan-of-record itself,
    preserving the no-refetch guarantee ``test_fetch_not_recalled_during_act``
    enforces.

    Raises:
        ApproveGateError: if ticket is not at either gate, session is missing,
            last_result is absent, last_result status is not an approval gate,
            or (with ``resolved_task``) the validated row vanished or its status
            diverged from the validated status.
        CwError: if no matching task is found.
    """
    from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES
    from cw.config import load_state
    from cw.dispatch import _should_gate_for_signoff

    store = load_dev_queue()
    task = _resolve_approval_target(store, ticket_id, client_name, resolved_task)

    if task.status not in _APPROVABLE_STATUSES:
        msg = (
            f"Cannot approve ticket '{ticket_id}': status is {task.status.value!r},"
            " expected BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF."
            " Use 'requeue' to re-run a stage."
        )
        raise ApproveGateError(msg)

    client_cfg = get_client(client_name)
    stages = client_cfg.pipeline.stages

    if task.stage not in stages:
        msg = (
            f"Cannot approve ticket '{ticket_id}':"
            f" stage {task.stage!r} not in pipeline."
        )
        raise ApproveGateError(msg)

    if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        from_stage = task.stage.value
        _clear_signoff_gate(task, stages)
        to_stage = task.stage.value
        save_dev_queue(store)
        return {
            "from_stage": from_stage,
            "to_stage": to_stage,
            "ticket_id": ticket_id,
            "client": client_name,
            "awaiting_signoff": False,
            "plan_requeued": False,
        }

    state = load_state()
    session = None
    if task.session_id is not None:
        session = state.find_by_name_or_id(task.session_id)

    if session is None:
        msg = (
            f"Cannot approve ticket '{ticket_id}': session not found"
            f" (session_id={task.session_id!r}). The session may have been"
            " cleaned up. Use 'requeue' to re-run the stage."
        )
        raise ApproveGateError(msg)

    if (
        session.last_result is None
        or session.last_result.get("status") not in SCOPE_GATED_APPROVAL_STATUSES
    ):
        actual = session.last_result.get("status") if session.last_result else None
        msg = (
            f"Cannot approve ticket '{ticket_id}': not at an approval gate"
            f" (last_result status={actual!r})."
            " Expected one of: plan_pending_approval, review_pending_approval."
            " Use 'requeue' if you want to re-run the current stage."
        )
        raise ApproveGateError(msg)

    if task.stage == stages[-1]:
        msg = (
            f"Cannot approve ticket '{ticket_id}':"
            f" already at terminal stage {task.stage!r}."
        )
        raise ApproveGateError(msg)

    from_stage = task.stage.value
    awaiting_signoff = False
    plan_requeued = False
    # Two independent gates share this branch (#968):
    #  - REVIEW-scoped signoff gate: reroutes the review->FINALIZE advance to
    #    AWAITING_OPERATOR_SIGNOFF (RFC 0007's "gate a ticket before it
    #    ships"). Never touches the plan_pending_approval->IMPL advance.
    #  - PLAN-scoped review-completeness gate: reroutes the
    #    plan_pending_approval->IMPL advance to a same-stage requeue when the
    #    plan-of-record was never quality-reviewed (Large-scope plans park
    #    for scope approval before the ambiguity scan / quality review /
    #    persistence steps run) -- prevents Stage 2 from spawning against an
    #    empty .cw/plan.md with no signoff markers.
    if task.stage == Stage.REVIEW and _should_gate_for_signoff(
        task, {client_name: client_cfg}
    ):
        transition_task_status(
            task,
            QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            disposition=SIGNOFF_GATE_DISPOSITION,
        )
        awaiting_signoff = True
    elif task.stage == Stage.PLAN and not (
        plan_reviewed if plan_reviewed is not None else _plan_is_reviewed(task)
    ):
        _reset_for_same_stage_requeue(task)
        plan_requeued = True
    else:
        _advance_task_pointer(task, stages)
    to_stage = task.stage.value

    save_dev_queue(store)

    return {
        "from_stage": from_stage,
        "to_stage": to_stage,
        "ticket_id": ticket_id,
        "client": client_name,
        "awaiting_signoff": awaiting_signoff,
        "plan_requeued": plan_requeued,
    }


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
