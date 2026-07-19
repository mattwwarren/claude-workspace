"""Dev-queue approval gates: plan/review approval + operator-signoff clearing.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
approve-gate entry point (``approve_ticket``), its lock-free body
(``_approve_ticket_locked``) shared with the RFC 0009 gate-recipe act phase,
and the physical-row resolver (``_resolve_approval_target``).

Layering: imports ``crud`` (``_find_ticket`` / ``_APPROVABLE_STATUSES``) and
``lifecycle`` (the transition + stage-advance helpers) at module level. The
``dev_queue ↔ dispatch`` cycle break — ``_should_gate_for_signoff`` — stays a
function-level deferred import inside ``_approve_ticket_locked``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import get_client
from cw.dev_queue.crud import _APPROVABLE_STATUSES, _find_ticket
from cw.dev_queue.lifecycle import (
    SIGNOFF_GATE_DISPOSITION,
    _advance_task_pointer,
    _clear_signoff_gate,
    _plan_is_reviewed,
    _reset_for_same_stage_requeue,
    transition_task_status,
)
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import ApproveGateError
from cw.models import QueueItemStatus, Stage

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask


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
