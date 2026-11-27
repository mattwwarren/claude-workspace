"""Requeue stage gate: validate, classify and apply a stage move (#3739)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.branch_ahead import merge_in_progress
from cw.dev_queue._requeue_messages import (
    _REQUEUE_FROM_CANCELLED_STATUSES,
    _REQUEUE_FROM_COMPLETED_STATUSES,
    _REQUEUE_FROM_FAILED_STATUSES,
    _pending_restage_hint,
    _requeue_state_error_message,
)
from cw.dev_queue.crud import _APPROVABLE_STATUSES, _validate_stage_in_pipeline
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    _advance_stage,
    _stage_regress,
)
from cw.dev_queue.next_commands import dev_queue_command
from cw.dev_queue.refusal import bound_session, refusal_hint, refusal_suffix_for_stages
from cw.dev_queue.requeue.plan_guard import (
    _impl_bypass_plan_available,
    _impl_bypass_worktree_path,
)
from cw.exceptions import RequeueStageError, RequeueStateError
from cw.models import QueueItemStatus, Stage
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask


def is_pending_requeue_noop(
    task: TicketTask, stage_override: str | None, allow_regress: bool
) -> bool:
    """True iff requeueing *task* is the idempotent PENDING no-op (#2692).

    The row is already PENDING, no ``--regress`` was asked for, and no
    ``--stage`` (or the row's own stage) was named. The single rule behind
    the CLI's lock-free exit, ``precheck_requeue(idempotent_pending=True)``
    and ``requeue_ticket(idempotent_pending=True)``.
    """
    return (
        task.status == QueueItemStatus.PENDING
        and not allow_regress
        and (stage_override is None or stage_override == task.stage.value)
    )


def _validate_requeue_stages(
    task: TicketTask, stages: list[Stage], target_stage: Stage
) -> None:
    """Both stages must be in the lane pipeline; refusals name the way out."""
    try:
        _validate_stage_in_pipeline(
            task.stage, stages, client=task.client, lane=task.lane
        )
    except RequeueStageError as exc:
        msg = (
            f"{exc} No --stage requeue can act on a row whose stage is not in its"
            " lane pipeline; fix the lane's pipeline.stages, or run"
            f" `{dev_queue_command('remove', task)}` and re-add the ticket."
        )
        raise RequeueStageError(msg) from exc
    try:
        _validate_stage_in_pipeline(
            target_stage, stages, client=task.client, lane=task.lane
        )
    except RequeueStageError as exc:
        legal = ", ".join(s.value for s in stages)
        msg = (
            f"{exc} Legal stages for this row: {legal}. Re-run the current stage"
            f" with `{dev_queue_command('requeue', task)}`, or name one of those"
            " stages with `--stage`."
        )
        raise RequeueStageError(msg) from exc


def _classify_requeue_move(
    task: TicketTask,
    stages: list[Stage],
    target_stage: Stage,
    *,
    allow_regress: bool,
    infer_regress: bool = False,
) -> Literal["regress", "forward"]:
    """The pure, lock-free stage gate of a requeue: regress or forward move.

    Validates both stages against the resolved pipeline and refuses the two
    backward moves that are never allowed: a regress without
    ``allow_regress`` (unless *infer_regress*, the CLI's #2692 opt-in, treats
    any backward target as a regress), and a regress of a row that is not
    parked. Shared by :func:`_apply_requeue_stage` and
    :func:`precheck_requeue` so the prediction and the real call cannot drift
    (#2517). "forward" includes the same-stage move. Refusals name the row's
    legal next command (#2692).
    """
    _validate_requeue_stages(task, stages, target_stage)
    if stages.index(target_stage) >= stages.index(task.stage):
        return "forward"
    # Both stages are proven in the pipeline: the position needs no config read.
    suffix = refusal_suffix_for_stages(task, bound_session(task), stages)
    hint = f"{suffix}{_pending_restage_hint(task, target_stage.value)}"
    if not (allow_regress or infer_regress):
        msg = (
            f"Cannot requeue ticket '{task.ticket_id}'"
            f" to stage '{target_stage.value}':"
            f" that would regress from '{task.stage.value}'."
            " Only same-stage or forward advancement is allowed."
            f" Use --regress to move backward.{hint}"
        )
        raise RequeueStageError(msg)
    if task.status not in _APPROVABLE_STATUSES:
        msg = (
            f"Cannot regress ticket '{task.ticket_id}'"
            f" to stage '{target_stage.value}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER or"
            f" AWAITING_OPERATOR_SIGNOFF.{hint}"
        )
        raise RequeueStageError(msg)
    return "regress"


def _forward_gate_applied(
    task: TicketTask,
    ticket_id: str,
    *,
    from_cancelled: bool,
    from_failed: bool,
    from_completed: bool,
    stage_override: str | None = None,
) -> tuple[bool, bool, bool]:
    """The forward/same-stage status gate; returns the three applied flags.

    The row must be at a BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF gate,
    OR (opt-in) CANCELLED/FAILED/COMPLETED; otherwise raises
    :class:`RequeueStateError`, naming the row's legal next command and, for a
    PENDING row asked for another *stage_override*, the remove/add route
    (#2692). Returns ``(from_cancelled_applied, from_failed_applied,
    from_completed_applied)``. Shared with :func:`precheck_requeue` (#2517).
    """
    approvable = task.status in _APPROVABLE_STATUSES
    cancelled_ok = from_cancelled and task.status in _REQUEUE_FROM_CANCELLED_STATUSES
    failed_ok = from_failed and task.status in _REQUEUE_FROM_FAILED_STATUSES
    completed_ok = from_completed and task.status in _REQUEUE_FROM_COMPLETED_STATUSES
    if not (approvable or cancelled_ok or failed_ok or completed_ok):
        msg = (
            f"{_requeue_state_error_message(ticket_id, task.status)}"
            f"{refusal_hint(task)}{_pending_restage_hint(task, stage_override)}"
        )
        raise RequeueStateError(msg)
    return cancelled_ok, failed_ok, completed_ok


def _apply_requeue_stage(
    task: TicketTask,
    stages: list[Stage],
    stage_override: str | None,
    client_cfg: ClientConfig,
    *,
    allow_regress: bool,
    infer_regress: bool = False,
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

    A forward bypass whose target is ``Stage.IMPL`` additionally requires an
    approved plan to be available (locally or on the tracker) -- see
    :func:`_impl_bypass_plan_available`. This guard is scoped to IMPL only:
    a missing plan is *fatal* there (the impl worker hard-exits
    ``plan_missing``), whereas REVIEW and FINALIZE degrade gracefully on a
    missing plan and are deliberately not guarded (GitHub #1681).
    """
    if stage_override is None:
        return False

    target_stage = Stage(stage_override)
    move = _classify_requeue_move(
        task,
        stages,
        target_stage,
        allow_regress=allow_regress,
        infer_regress=infer_regress,
    )

    if move == "regress":
        # #2421: this path carries no blocker at all, so the merge-caused flag
        # can only come from a live measurement. Probed only where
        # _stage_regress would stamp it (FINALIZE origin).
        merge_conflict_detected = (
            task.stage == Stage.FINALIZE
            and merge_in_progress(resolve_task_worktree(task, client_cfg)) is True
        )
        _stage_regress(
            task, target_stage, merge_conflict_detected=merge_conflict_detected
        )
        return True

    # Guarded because impl hard-exits plan_missing; review and finalize
    # degrade and are deliberately not guarded (see #1681 Decisions).
    if target_stage == Stage.IMPL and stages.index(target_stage) > stages.index(
        task.stage
    ):
        plan_check = _impl_bypass_plan_available(task, client_cfg)
        if not plan_check.available:
            wt_path = _impl_bypass_worktree_path(task, client_cfg)
            if plan_check.tracker_checked:
                availability_clause = (
                    " is missing or stale, and no reviewed plan comment"
                    f" ('{_PLAN_SPEC_MARKER}' + '{_PLAN_SOUNDNESS_MARKER}')"
                    " was found on the tracker."
                )
                remediation_clause = (
                    " Let Stage 1 (plan) run and post its approved plan"
                    " first, or requeue at --stage plan instead."
                )
            else:
                # #1906: fetch_approved_plan_comment is GitHub-only -- a
                # known non-GitHub tracker (e.g. linear) was never actually
                # queried, so the message must not claim it was checked, and
                # must not tell the operator to regenerate a plan that may
                # already be posted and approved on that tracker.
                availability_clause = (
                    " is missing or stale, and the configured tracker"
                    f" ({plan_check.tracker!r}) was not checked for a"
                    " reviewed plan comment -- tracker-side plan recovery"
                    " for this tracker is not implemented by this guard."
                )
                remediation_clause = (
                    " Verify whether an approved plan is already posted on"
                    f" the {plan_check.tracker!r} tracker before requeuing;"
                    " if so, Stage 2's tracker-aware plan recovery can pick"
                    " it up directly instead of re-running Stage 1 (plan)."
                )
            replan = dev_queue_command("requeue", task, "--stage", Stage.PLAN.value)
            msg = (
                f"Cannot requeue ticket '{task.ticket_id}' to stage 'impl':"
                f" no approved plan is available. '{wt_path / '.cw' / 'plan.md'}'"
                f"{availability_clause}{remediation_clause}"
                f" To re-run planning: `{replan}`."
            )
            raise RequeueStageError(msg)

    # Forward or same-stage: caller enforces the BLOCKED_ON_USER precondition.
    # Forward stage move → direction="advance"; the same-stage case is naturally
    # guarded silent by _emit_stage_change's old==new check. RFC 0008 W1.
    _advance_stage(task, stages, target_stage)
    return False
