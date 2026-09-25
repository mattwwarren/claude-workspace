"""Dev-queue approval gates: plan/review approval + operator-signoff clearing.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
approve-gate entry point (``approve_ticket``), its lock-free body
(``_approve_ticket_locked``) shared with the RFC 0009 gate-recipe act phase,
and the physical-row resolver (``_resolve_approval_target``). Also owns the
``plan_scope_drift`` grant (``approve_scope_drift_ticket``, #2337), a separate
entry point because that park carries no approval-gate status or disposition.

Layering: imports ``crud`` (``_find_ticket`` / ``_APPROVABLE_STATUSES``) and
``lifecycle`` (the transition + stage-advance helpers) at module level. The
``dev_queue ↔ dispatch`` cycle break — ``_should_gate_for_signoff``,
``_should_force_hold_finalize``, and (#1617) ``_resolve_scope_tier`` /
``_extract_scope_tier`` — stays a function-level deferred import inside
``_approve_ticket_locked``. (#1640) ``_APPROVAL_GATE_REASON`` and
``SCOPE_GATED_APPROVAL_STATUSES`` are deferred imports inside
``_not_at_approval_gate`` instead, the sole site that now consumes them.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from cw.atomic import atomic_write_text
from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.config import dev_queue_file, get_client
from cw.dev_queue.crud import _APPROVABLE_STATUSES, _find_ticket
from cw.dev_queue.lifecycle import (
    BRANCH_STALENESS_GATE_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    _advance_task_pointer,
    _clear_signoff_gate,
    _plan_is_reviewed,
    _reset_for_same_stage_requeue,
)
from cw.dev_queue.plan_promotion import promote_plan_draft
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.gh import branch_head_sha_on_origin
from cw.models import (
    PLAN_APPROVED_FINGERPRINT_KEY,
    PLAN_DRAFT_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore, Session, TicketTask


_log = logging.getLogger(__name__)
_SCOPE_DRIFT_RECOVERY_MARKER = "scope-drift-approval-recovery.jsonl"


def approve_ticket(ticket_id: str, client_name: str) -> dict[str, str | bool | None]:
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
    rather than advancing/completing it), plan_requeued (True iff *this*
    call re-parked a PLAN-stage ticket at Stage.PLAN/PENDING instead of
    advancing to IMPL, because the plan-of-record was not yet quality-
    reviewed -- see GitHub #968; always present, False on every other path),
    finalize_held (RFC 0011 A3, #1160; always False on this entry point,
    which is the human release path -- see ``_approve_ticket_locked``),
    plan_approved_fingerprint (#2102; the draft fingerprint this approval is
    bound to, read from the approving session's sentinel -- None on every
    non-PLAN path and whenever the sentinel carried no fingerprint), and
    plan_promoted (#2342; True iff *this* call's direct plan->impl advance
    promoted ``.cw/plan-draft.md`` to ``.cw/plan.md`` -- always present, False
    on every other path and when there was no draft to promote).

    Raises:
        ApproveGateError: if ticket is not at either gate, session is missing,
            last_result is absent, last_result status is not an approval gate,
            or promoting the plan draft failed on I/O (nothing is recorded).
        CwError: if no matching task is found.
    """
    with _lock():
        # operator_initiated=True: this entry point IS the human `cw dev-queue
        # approve` path, the one caller authorised to release an RFC 0011 A3
        # force hold (#1160).
        return _approve_ticket_locked(ticket_id, client_name, operator_initiated=True)


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


def _record_approve_scope_routing_decision(
    ticket_id: str,
    client_name: str,
    task: TicketTask,
    session: Session,
    *,
    finalize_held: bool,
    awaiting_signoff: bool,
    plan_requeued: bool,
) -> None:
    """Emit the #1617 scope-routing audit event for the gate-release site (D4).

    Extracted from ``_approve_ticket_locked`` to keep that function under the
    PLR0912/PLR0915 branch/statement ceilings. This site has no ``last_result``
    parameter and no ``_resolve_scope_tier`` call of its own (unlike the three
    ``routing.py`` park-decision sites), so both the sentinel's ``scope.tier``
    and the resolved tier are sourced from the owning session's
    ``last_result`` here. ``disposition`` is a literal describing which of the
    caller's four branches actually fired -- NOT ``task.disposition``, since
    the ``finalize_held`` branch performs no mutation at all (the row stays
    parked exactly as it is), so ``task.disposition`` would not reflect it.

    The ``"finalize_hold_branch"``/``"signoff_branch"`` literals below are
    deliberately spelled distinct from
    ``lifecycle.FINALIZE_GATE_HELD_DISPOSITION``
    (``"finalize_gate_held"``)/``lifecycle.SIGNOFF_GATE_DISPOSITION``
    (``"signoff_gate"``) -- this function's four branches describe *which
    code path fired inside* ``_approve_ticket_locked``, a different semantic
    axis from ``task.disposition`` at the ``routing.py`` sites, and reusing
    those constants directly would collapse that distinction. See Checkpoint
    3a review, #1617.
    """
    from cw.dispatch import _RULE_GATE_RELEASE, _extract_scope_tier, _resolve_scope_tier

    if finalize_held:
        disposition = "finalize_hold_branch"
    elif awaiting_signoff:
        disposition = "signoff_branch"
    elif plan_requeued:
        disposition = "plan_requeued"
    else:
        disposition = "advanced"
    record_event(
        OrchestratorEventType.SCOPE_ROUTING_DECISION,
        {
            "ticket_id": ticket_id,
            "client": client_name,
            "scope_hint": task.scope_hint,
            "sentinel_tier": _extract_scope_tier(session.last_result),
            "resolved_tier": _resolve_scope_tier(session.last_result, task),
            "rule": _RULE_GATE_RELEASE,
            "disposition": disposition,
        },
        correlation_id=ticket_id,
    )


def _stamp_plan_approval(
    task: TicketTask, from_stage: str, session: Session
) -> str | None:
    """Record the tracker-neutral plan-approval fact (schema v35/v36).

    Stamped on BOTH the #968 same-stage re-park and the direct advance, since
    either means the operator (or an enabled gate recipe) released this row's
    ``plan_pending_approval`` gate. ``spawn.py`` threads it into the worker's
    ``queue_metadata`` so ``auto-dev-plan.md``'s Checkpoint 1 can honor it as
    approval evidence on a tracker the GitHub-only ``--post-marker`` comment
    never reaches (Linear). Runs before ``save_dev_queue`` so it lands in the
    same durable write as the status transition. Extracted from
    ``_approve_ticket_locked`` to keep that function under the PLR0915
    statement ceiling, like its sibling helpers above.

    The v36 companion ``plan_approved_fingerprint`` (#2102) is read from the
    approving session's sentinel and stamped in the same branch, binding the
    approval to the draft the operator actually read. Both writes are gated on
    the PLAN stage together: a fingerprint without a timestamp records an
    approval that never happened, and a timestamp without a fingerprint is the
    unbound approval this field exists to eliminate. A sentinel that omits the
    key (pre-#2102 producer) stamps None — recorded absence, not a wildcard.

    Returns what THIS call stamped, which is what the caller reports back under
    ``plan_approved_fingerprint``: None on every non-PLAN path, where reading
    the field off the row instead would report whatever some *earlier* plan
    approval left there as though this approval had bound it.
    """
    if from_stage != Stage.PLAN.value:
        return None
    task.plan_approved_at = datetime.now(UTC)
    fingerprint = (session.last_result or {}).get(PLAN_DRAFT_FINGERPRINT_KEY)
    task.plan_approved_fingerprint = (
        fingerprint if isinstance(fingerprint, str) else None
    )
    return task.plan_approved_fingerprint


def _promote_plan_draft_on_direct_advance(
    task: TicketTask,
    client_cfg: ClientConfig,
    *,
    plan_reviewed: bool | None,
) -> bool:
    """Promote the approved plan draft on the direct plan->impl advance (#2342).

    Only the public/CLI path (``plan_reviewed is None``) promotes: the trusted
    gate-recipe caller passes ``plan_reviewed=True`` for an auto-adopted plan
    and never approved a draft. Extracted from ``_approve_ticket_locked`` to
    keep that function under the PLR0915 statement ceiling, like its sibling
    helpers above. Runs before ``_advance_task_pointer`` and
    ``save_dev_queue``, so a raised ``ApproveGateError`` records nothing.
    """
    if task.stage != Stage.PLAN or plan_reviewed is not None:
        return False
    return promote_plan_draft(task, client_cfg)


def _not_at_approval_gate(session: Session, task: TicketTask) -> bool:
    """True iff neither release condition for the approval gate is met.

    Extracted from ``_approve_ticket_locked`` to keep that function under the
    PLR0915 statement ceiling (#1640), following the same extraction pattern
    as ``_record_approve_scope_routing_decision`` above. Two independent
    conditions can satisfy the gate: the session's ``last_result`` status is
    one of ``SCOPE_GATED_APPROVAL_STATUSES`` (the ``plan_pending_approval`` /
    ``review_pending_approval`` release path), or the task's ``disposition``
    records a park armed by the ``scope_hint`` escalation gate
    (``_APPROVAL_GATE_REASON``, GitHub #1640). ``approve`` proceeds if either
    condition holds -- unless the #1823 branch-staleness override below fires
    first, which vetoes both.
    """
    from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES
    from cw.dispatch import _APPROVAL_GATE_REASON

    # #1823: ahead of both conditions below, and returning True unconditionally
    # rather than joining the `and`. A branch-staleness park leaves the
    # *sentinel* untouched -- session.last_result.status is still
    # "review_pending_approval" -- and only diverges task.disposition. So
    # not_at_status_gate is False and not_at_disposition_gate is True, the
    # `and` yields False, and the row would read as "at the approval gate":
    # `approve` would release a ticket whose reviewed tree no longer matches
    # origin/<default_branch>. The gate must fail closed until the branch is
    # rebased; recovery is `cw dev-queue requeue`/`drain`, not `approve`.
    if task.disposition == BRANCH_STALENESS_GATE_DISPOSITION:
        return True

    # #2123: chained immediately after #1823's override, on identical
    # reasoning. A review-staleness park likewise leaves the sentinel reading
    # "review_pending_approval" and diverges only task.disposition, so without
    # this the row reads as "at the approval gate" and `approve` would release
    # a tree that no reviewer ran against. That is the exact incident: a
    # review-stage park released via `requeue` can return to this status with
    # stale artifacts. Recovery is `cw dev-queue requeue`/`drain` (which
    # re-runs review), not `approve`.
    if task.disposition == REVIEW_STALENESS_GATE_DISPOSITION:
        return True

    not_at_status_gate = (
        session.last_result is None
        or session.last_result.get("status") not in SCOPE_GATED_APPROVAL_STATUSES
    )
    not_at_disposition_gate = task.disposition != _APPROVAL_GATE_REASON
    return not_at_status_gate and not_at_disposition_gate


def _raise_stage_not_in_pipeline(
    ticket_id: str, task: TicketTask, stages: list[Stage], client_name: str
) -> None:
    """Raise ``ApproveGateError`` for a stage absent from the resolved pipeline.

    Extracted to keep ``_approve_ticket_locked`` under ruff's PLR0915
    statement-count gate -- the message names the lane and its resolved
    stages so a lane pipeline.stages override is never silently reported as
    a client-default mismatch (#2216).
    """
    msg = (
        f"Cannot approve ticket '{ticket_id}':"
        f" stage {task.stage!r} not in pipeline for lane {task.lane!r} of"
        f" client {client_name!r}. Lane pipeline stages: {stages}."
    )
    raise ApproveGateError(msg)


def _require_approval_session(ticket_id: str, task: TicketTask) -> Session:
    """Return the session that parked *task*, or raise ``ApproveGateError``.

    Extracted to keep ``_approve_ticket_locked`` under ruff's PLR0915
    statement-count gate, like ``_raise_stage_not_in_pipeline`` above.
    """
    from cw.config import load_state

    session = None
    if task.session_id is not None:
        session = load_state().find_by_name_or_id(task.session_id)
    if session is None:
        msg = (
            f"Cannot approve ticket '{ticket_id}': session not found"
            f" (session_id={task.session_id!r}). The session may have been"
            " cleaned up. Use 'requeue' to re-run the stage."
        )
        raise ApproveGateError(msg)
    return session


def _approve_ticket_locked(
    ticket_id: str,
    client_name: str,
    *,
    resolved_task: TicketTask | None = None,
    plan_reviewed: bool | None = None,
    operator_initiated: bool = False,
) -> dict[str, str | bool | None]:
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
    :func:`_plan_is_reviewed` check (tracker-gated and worktree-resolving via
    the client config, so a Linear-tracked row never pays a doomed ``gh``
    call and a dispatch-driven row -- whose ``worktree_path`` is never
    stamped -- still finds its on-disk ``.cw/plan.md``); the trusted
    gate-recipe caller (``gate_recipes._act_auto_adopt_plan``) passes
    ``plan_reviewed=True``
    explicitly so this function never re-fetches the plan-of-record itself,
    preserving the no-refetch guarantee ``test_fetch_not_recalled_during_act``
    enforces.

    ``operator_initiated`` (RFC 0011 A3, GitHub #1160) records caller
    provenance for the proactive finalize hold. ``True`` means "a human typed
    ``cw dev-queue approve``" -- the one caller authorised to RELEASE an armed
    hold, so the force-hold check is skipped entirely and the call falls
    through to the unchanged signoff/plan/advance chain. Every automatic caller
    (the RFC 0009 gate-recipe reactor, and any future one) simply omits the
    kwarg.

    The default direction is deliberately the fail-safe one, mirroring
    ``plan_reviewed``'s "trusted caller passes explicitly" shape but inverted:
    a caller that FORGETS the kwarg is treated as automatic and the ticket
    stays held. The opposite default would let a new call site silently ship a
    ticket its operator had explicitly asked to stop.

    When the hold fires, this function performs NO mutation at all -- the row
    is already parked and stays exactly as it is -- and reports
    ``finalize_held=True`` so the caller can emit its own correction event.

    On the direct plan->impl advance of the public/CLI path
    (``plan_reviewed is None``), an approved ``.cw/plan-draft.md`` is promoted
    to ``.cw/plan.md`` before the stage pointer moves (#2342), and reported
    as ``plan_promoted``. A promotion I/O failure raises before any mutation
    or ``save_dev_queue``, so the row stays parked and nothing is recorded.

    Raises:
        ApproveGateError: if ticket is not at either gate, session is missing,
            last_result is absent, last_result status is not an approval gate,
            (with ``resolved_task``) the validated row vanished or its status
            diverged from the validated status, or promoting the plan draft
            failed on I/O.
        CwError: if no matching task is found.
    """
    from cw.dispatch import (
        _park_signoff_gate,
        _should_force_hold_finalize,
        _should_gate_for_signoff,
    )
    from cw.executor import resolve_pipeline_stages

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
    stages = resolve_pipeline_stages(task, client_cfg)

    if task.stage not in stages:
        _raise_stage_not_in_pipeline(ticket_id, task, stages, client_name)

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
            "finalize_held": False,
            # Never the row's stored value: clearing a signoff gate stamps no
            # plan approval, so reporting one would credit this call with a
            # binding an earlier PLAN approval made.
            PLAN_APPROVED_FINGERPRINT_KEY: None,
            # Likewise: clearing a signoff gate never approves a plan draft,
            # so there is never a draft for this call to have promoted.
            "plan_promoted": False,
        }

    session = _require_approval_session(ticket_id, task)

    if _not_at_approval_gate(session, task):
        actual = session.last_result.get("status") if session.last_result else None
        msg = (
            f"Cannot approve ticket '{ticket_id}': not at an approval gate"
            f" (disposition={task.disposition!r}, last_result status={actual!r})."
            " Expected disposition 'approval_gate', or last_result status one of:"
            " plan_pending_approval, review_pending_approval."
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
    finalize_held = False
    plan_promoted = False
    # Three independent gates share this branch (#968, #1160):
    #  - REVIEW-scoped A3 force hold: a proactive "do not ship this
    #    unattended", checked FIRST and only for an automatic caller. It makes
    #    no mutation -- the row stays parked exactly as it is -- so an
    #    automatic approve degrades to a no-op instead of shipping the ticket.
    #  - REVIEW-scoped signoff gate: reroutes the review->FINALIZE advance to
    #    AWAITING_OPERATOR_SIGNOFF (RFC 0007's "gate a ticket before it
    #    ships"). Never touches the plan_pending_approval->IMPL advance.
    #  - PLAN-scoped review-completeness gate: reroutes the
    #    plan_pending_approval->IMPL advance to a same-stage requeue when the
    #    plan-of-record was never quality-reviewed (Large-scope plans park
    #    for scope approval before the ambiguity scan / quality review /
    #    persistence steps run) -- prevents Stage 2 from spawning against an
    #    empty .cw/plan.md with no signoff markers.
    if (
        task.stage == Stage.REVIEW
        and not operator_initiated
        and _should_force_hold_finalize(task, {client_name: client_cfg})
    ):
        finalize_held = True
    elif task.stage == Stage.REVIEW and _should_gate_for_signoff(
        task, {client_name: client_cfg}
    ):
        _park_signoff_gate(task)
        awaiting_signoff = True
    elif task.stage == Stage.PLAN and not (
        plan_reviewed
        if plan_reviewed is not None
        else _plan_is_reviewed(task, client_cfg)
    ):
        _reset_for_same_stage_requeue(task)
        plan_requeued = True
    else:
        plan_promoted = _promote_plan_draft_on_direct_advance(
            task, client_cfg, plan_reviewed=plan_reviewed
        )
        _advance_task_pointer(task, stages)
    stamped_fingerprint = _stamp_plan_approval(task, from_stage, session)
    to_stage = task.stage.value

    # #1617 (D4): _approve_ticket_locked is a gate-release site, excluded from
    # the scope_hint park-decision gate (Scope item 1) but still covered by
    # the scope-routing audit trail (Scope item 2). save_dev_queue runs first
    # so the audit event never durably claims a disposition that did not
    # actually land in the dev-queue store (Checkpoint 3a review, #1617): if
    # save_dev_queue raises or the process dies between the two calls, no
    # audit event is emitted for a mutation that never persisted.
    save_dev_queue(store)

    _record_approve_scope_routing_decision(
        ticket_id,
        client_name,
        task,
        session,
        finalize_held=finalize_held,
        awaiting_signoff=awaiting_signoff,
        plan_requeued=plan_requeued,
    )

    return {
        "from_stage": from_stage,
        "to_stage": to_stage,
        "ticket_id": ticket_id,
        "client": client_name,
        "awaiting_signoff": awaiting_signoff,
        "plan_requeued": plan_requeued,
        "finalize_held": finalize_held,
        PLAN_APPROVED_FINGERPRINT_KEY: stamped_fingerprint,
        "plan_promoted": plan_promoted,
    }


class ScopeDriftApproval(TypedDict):
    """What :func:`approve_scope_drift_ticket` stamped and where it moved."""

    from_stage: str
    to_stage: str
    ticket_id: str
    client: str
    extra_files: list[str]
    approved_head: str


def _write_scope_drift_recovery_marker(payload: dict[str, object]) -> None:
    """Persist an operator-visible marker when approval audit repair is uncertain."""
    path = dev_queue_file().with_name(_SCOPE_DRIFT_RECOVERY_MARKER)
    try:
        previous = path.read_text() if path.exists() else ""
        atomic_write_text(path, previous + json.dumps(payload, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        # The marker is the last-resort recovery path. If even it is unavailable,
        # the critical log is the remaining operator alert; never hide the
        # original queue/audit inconsistency behind a marker-write exception.
        _log.critical(
            "scope-drift approval recovery marker could not be persisted: %s",
            payload,
            exc_info=True,
        )


def _emit_scope_drift_compensation(
    ticket_id: str,
    client_name: str,
    approval_payload: dict[str, object],
    error: Exception,
    *,
    rolled_back: bool,
) -> None:
    """Correct a possibly-written approval event, failing loudly if repair fails."""
    payload = {
        "ticket_id": ticket_id,
        "client": client_name,
        "approval_event": OrchestratorEventType.TICKET_APPROVED.value,
        "approval_payload": approval_payload,
        "error": str(error),
        "rolled_back": rolled_back,
        "recovery_required": not rolled_back,
    }
    try:
        record_event(
            OrchestratorEventType.TICKET_APPROVAL_FAILED,
            payload,
            correlation_id=ticket_id,
        )
    except Exception:  # noqa: BLE001
        # A failed compensating write must not silently leave a potentially
        # false TICKET_APPROVED event standing alone.
        _write_scope_drift_recovery_marker(payload)
        _log.critical(
            "scope-drift approval audit compensation failed for %s/%s",
            client_name,
            ticket_id,
            exc_info=True,
        )


def approve_scope_drift_ticket(
    ticket_id: str,
    client_name: str,
    extra_files: list[str],
) -> ScopeDriftApproval:
    """Grant operator-directed scope growth to a ``plan_scope_drift`` park (#2337).

    The row must be BLOCKED_ON_USER at Stage.IMPL with ``blocked_reason``
    ``plan_scope_drift`` -- a park routed by the generic Rule 5 fallthrough,
    which stamps no ``disposition``, so :func:`approve_ticket`'s gate
    predicate can never match it. Stamps the sorted, deduped *extra_files* and
    the branch's current origin HEAD SHA (the binding gate 2 checks by
    ancestry), then re-queues IMPL in place so a fresh session re-runs gate 2
    with the grant in its ``queue_metadata``.

    Returns a :class:`ScopeDriftApproval` (extra_files as stamped).

    Raises:
        ApproveGateError: if the row is not parked for plan_scope_drift at
            IMPL, *extra_files* is empty, or the branch head cannot be
            resolved on origin. Nothing is mutated on any of these paths.
        CwError: if no matching task is found.
    """
    with _lock():
        return _approve_scope_drift_locked(
            ticket_id,
            client_name,
            extra_files,
        )


def _approve_scope_drift_locked(
    ticket_id: str,
    client_name: str,
    extra_files: list[str],
) -> ScopeDriftApproval:
    """Lock-free body of :func:`approve_scope_drift_ticket`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``), as for
    :func:`_approve_ticket_locked`. Every validation, including the ``gh``
    head lookup, runs before the first mutation, so a refusal leaves the row
    exactly as it was.
    """
    store = load_dev_queue()
    task = _resolve_approval_target(store, ticket_id, client_name, None)

    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER."
        )
        raise ApproveGateError(msg)
    if task.stage != Stage.IMPL:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': stage is"
            f" {task.stage.value!r}, expected 'impl' (plan_scope_drift parks"
            " at Step 2.5 of the IMPL stage)."
        )
        raise ApproveGateError(msg)
    if task.blocked_reason != PLAN_SCOPE_DRIFT_BLOCKER_REASON:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}':"
            f" blocked_reason is {task.blocked_reason!r}, expected"
            f" {PLAN_SCOPE_DRIFT_BLOCKER_REASON!r}. Use plain 'approve' or"
            " 'requeue' for other parks."
        )
        raise ApproveGateError(msg)
    approved_files = sorted(set(extra_files))
    if not approved_files:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': no extra"
            " files given. Pass the repo-relative paths to allow, comma-separated."
        )
        raise ApproveGateError(msg)

    client_cfg = get_client(client_name)
    branch = f"{client_cfg.feature_branch_prefix}/{ticket_id}"
    head_sha, _gh_available = branch_head_sha_on_origin(
        branch, cwd=_git_dir(client_cfg)
    )
    if head_sha is None:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': could not"
            f" resolve the head of origin/{branch} (gh unavailable, branch not"
            " pushed, or a transient error) -- the approval has nothing to be"
            " bound to."
        )
        raise ApproveGateError(msg)

    original_store = store.model_copy(deep=True)
    from_stage = task.stage.value
    task.scope_drift_approved_extra_files = approved_files
    task.scope_drift_approved_head = head_sha
    _reset_for_same_stage_requeue(task)
    approval_payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client_name,
        "from_stage": from_stage,
        "to_stage": task.stage.value,
        SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY: approved_files,
        SCOPE_DRIFT_APPROVED_HEAD_KEY: head_sha,
    }
    save_dev_queue(store)
    try:
        record_event(
            OrchestratorEventType.TICKET_APPROVED,
            approval_payload,
        )
    except Exception as event_error:
        try:
            save_dev_queue(original_store)
        except Exception as rollback_error:
            recovery_payload = {
                "ticket_id": ticket_id,
                "client": client_name,
                "approval_payload": approval_payload,
                "event_error": str(event_error),
                "rollback_error": str(rollback_error),
                "recovery_required": True,
            }
            _write_scope_drift_recovery_marker(recovery_payload)
            _emit_scope_drift_compensation(
                ticket_id,
                client_name,
                approval_payload,
                rollback_error,
                rolled_back=False,
            )
            _log.critical(
                "scope-drift approval rollback failed for %s/%s; operator recovery"
                " is required",
                client_name,
                ticket_id,
                exc_info=True,
            )
            raise rollback_error from event_error
        _emit_scope_drift_compensation(
            ticket_id,
            client_name,
            approval_payload,
            event_error,
            rolled_back=True,
        )
        raise
    return {
        "from_stage": from_stage,
        "to_stage": task.stage.value,
        "ticket_id": ticket_id,
        "client": client_name,
        "extra_files": approved_files,
        "approved_head": head_sha,
    }
