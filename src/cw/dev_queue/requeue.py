"""Dev-queue requeue + unblock: re-run a stage, regress, or clear a salvage park.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
stage-resolution helper (``_apply_requeue_stage``), the forward-gate error
message builder (``_requeue_state_error_message``), the ``requeue_ticket``
entry point, and ``unblock_ticket`` (salvage-park clearing).

Layering: imports ``crud`` (``_find_ticket`` / ``_APPROVABLE_STATUSES``) and
``lifecycle`` (the transition + stage helpers) at module level. The
``dev_queue ↔ config`` cycle break — ``load_state`` / ``save_state`` /
``sessions_lock`` / ``ReapReason`` — stays a function-level deferred import
inside ``unblock_ticket``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.config import get_client
from cw.dev_queue.crud import (
    _APPROVABLE_STATUSES,
    _find_ticket,
    _validate_stage_in_pipeline,
)
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    _emit_stage_change,
    _plan_body_signoff_ok,
    _raise_stage_high_water,
    _reset_for_same_stage_requeue,
    _stage_regress,
    transition_task_status,
)
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import RequeueStageError, RequeueStateError, UnblockStateError
from cw.gh import fetch_approved_plan_comment
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _checked_out_branch, worktree_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

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


def _impl_bypass_worktree_path(task: TicketTask, client_cfg: ClientConfig) -> Path:
    """Worktree path the impl-bypass guard would find for ``task``.

    Single source for the branch-name/worktree-path formula shared by
    :func:`_impl_bypass_plan_available` (which uses it to check for
    ``.cw/plan.md``) and the guard's own error message in
    :func:`_apply_requeue_stage` (which cites the same path in its refusal
    text) -- keeps the two from drifting independently. See GitHub #1681.
    """
    branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
    return worktree_path_for(client_cfg, branch)


class _ImplBypassPlanCheck(NamedTuple):
    """Verdict from :func:`_impl_bypass_plan_available` (GitHub #1906).

    A named 3-tuple rather than a bare ``bool`` so the caller can build an
    honest refusal message without re-deriving tracker state. ``tracker_checked``
    is True only when :func:`fetch_approved_plan_comment` (GitHub-only) was
    actually called -- i.e. the tracker was ``None`` (unresolvable, fail-open)
    or positively ``github-issues``. It is False when a *known, non-GitHub*
    tracker (e.g. ``linear``) caused the call to be skipped -- that case must
    not be described as "no reviewed plan comment ... was found on the
    tracker", since the tracker was never actually asked.
    """

    available: bool
    tracker_checked: bool
    tracker: str | None


def _impl_bypass_plan_available(
    task: TicketTask, client_cfg: ClientConfig
) -> _ImplBypassPlanCheck:
    """Verdict on whether an approved plan is available for a forward bypass
    to IMPL.

    Local-first, tracker-fallback -- the inverse order of
    :func:`cw.dev_queue.lifecycle._plan_is_reviewed` (tracker-first,
    ``.cw/plan.md``-fallback). That predicate targets ``task.worktree_path``,
    which is always ``None`` for dispatch-driven ``TicketTask`` rows (dispatch
    stamps ``session_id`` but not ``worktree_path`` -- see
    ``queue_peek.py:298-299``), so it would never see the real on-disk
    worktree and would always pay for a network call on the common path. This
    predicate instead computes the real worktree path via
    :func:`worktree_path_for` + :func:`_checked_out_branch` -- the same
    read-only primitives :func:`cw.worktree.create_worktree` uses to decide
    reuse-vs-rebuild -- so the common "reused worktree, no rebuild" case is
    resolved with zero network cost. Only falls through to
    :func:`fetch_approved_plan_comment` when the local read fails to prove
    the plan is there (worktree missing, foreign branch, or no/unmarked
    ``.cw/plan.md``). See GitHub #1681.

    :func:`fetch_approved_plan_comment` is GitHub-only (``src/cw/gh.py``) --
    calling it against a Linear-tracked (or other non-GitHub) client silently
    returns ``None`` (a ``gh`` call against a non-GitHub-shaped ticket id
    just fails), which previously masqueraded as "the tracker was checked and
    had no reviewed comment" (#1906). The fallthrough is now gated on the
    resolved tracker: fail-open (attempt the GitHub fetch, matching prior
    behavior) when ``resolve_tracker`` returns ``None`` (unresolvable/absent
    config -- verified backward-compatible against every existing test, all
    of which resolve ``tracker=None``); skip the call entirely, honestly,
    when the tracker is *positively* known to be non-GitHub.
    """
    branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
    wt_path = _impl_bypass_worktree_path(task, client_cfg)
    if wt_path.exists() and _checked_out_branch(wt_path) == branch:
        plan_path = wt_path / ".cw" / "plan.md"
        try:
            body = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            body = None
        if body is not None and _plan_body_signoff_ok(body):
            return _ImplBypassPlanCheck(True, tracker_checked=False, tracker=None)

    tracker = resolve_tracker(client_cfg.workspace_path)
    if tracker is not None and tracker != TRACKER_GITHUB_ISSUES:
        return _ImplBypassPlanCheck(False, tracker_checked=False, tracker=tracker)

    tracker_body = fetch_approved_plan_comment(task.ticket_id)
    available = tracker_body is not None and _plan_body_signoff_ok(tracker_body)
    return _ImplBypassPlanCheck(available, tracker_checked=True, tracker=tracker)


def _apply_requeue_stage(
    task: TicketTask,
    stages: list[Stage],
    stage_override: str | None,
    client_cfg: ClientConfig,
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
    _validate_stage_in_pipeline(target_stage, stages, client=task.client)

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

    # Guarded because impl hard-exits plan_missing; review and finalize
    # degrade and are deliberately not guarded (see #1681 Decisions).
    if target_stage == Stage.IMPL and target_idx > current_idx:
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
            msg = (
                f"Cannot requeue ticket '{task.ticket_id}' to stage 'impl':"
                f" no approved plan is available. '{wt_path / '.cw' / 'plan.md'}'"
                f"{availability_clause}{remediation_clause}"
            )
            raise RequeueStageError(msg)

    # Forward or same-stage: caller enforces the BLOCKED_ON_USER precondition.
    old_stage = task.stage
    task.stage = target_stage
    _raise_stage_high_water(task, stages, target_stage)
    # Forward stage move → direction="advance"; the same-stage case is naturally
    # guarded silent by _emit_stage_change's old==new check. RFC 0008 W1.
    _emit_stage_change(task, old_stage, target_stage, "advance")
    return False


class _ReviewDeliverability(NamedTuple):
    """Verdict from :func:`_review_reentry_deliverable` (GitHub #1730).

    A named 4-tuple rather than a bare one so a future reorder of fields is a
    mypy-catchable error at every call/unpack site instead of a silent bug.
    """

    deliverable: bool
    reason: str
    backend: str
    tracker: str | None


def _review_reentry_deliverable(
    task: TicketTask, client_cfg: ClientConfig
) -> _ReviewDeliverability:
    """True iff a REVIEW-stage re-entry can deliver operator tracker context.

    Returns a :class:`_ReviewDeliverability`. ``backend`` and ``tracker`` are
    always populated (even when ``deliverable`` is True, or when ``tracker``
    was never consulted because ``backend`` alone already settled the
    verdict) so the caller can fold them into the
    ``REQUEUE_REVIEW_DELIVERY_DEGRADED`` payload for machine consumption
    (GitHub #1730) without a second, possibly-drifting resolution call.
    ``tracker`` is ``None`` when unresolvable (no ``tracking.primary.system``
    in ``.claude/project-config.yaml`` -- see ``cw.tracker.resolve_tracker``'s
    own contract) or not meaningful for the resolved backend (``claude-native``
    never consults it). ``resolve_tracker`` is called unconditionally rather
    than only on the codex branch: it is a local, read-only config-file read
    with no network I/O, so a consistent payload key set is worth more than
    skipping it.

    Deferred import of ``resolve_executor_config`` to break the ``dev_queue ->
    executor -> codex_background -> dev_queue`` cycle (same precedent as
    ``unblock_ticket``'s deferred ``cw.config`` import, see module docstring).
    NOTE for test authors: because these imports are function-local, tests must
    monkeypatch ``cw.executor.resolve_executor_config`` /
    ``cw.tracker.resolve_tracker`` at their origin modules, not this module --
    there is no module-level attribute of either name here to patch.

    Unlike an #1681-style guard, this NEVER blocks the transition (see
    ``requeue.py``'s "review and finalize degrade and are deliberately not
    guarded" comment in ``_apply_requeue_stage``, and #1730/#1717 comment 6):
    the caller emits a loud event and proceeds regardless.
    """
    from cw.executor import resolve_executor_config
    from cw.models.orchestrator_config import CLAUDE_NATIVE_BACKEND, CODEX_BACKEND
    from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

    backend = resolve_executor_config(Stage.REVIEW, task, client_cfg).backend
    tracker = resolve_tracker(client_cfg.workspace_path)
    if backend == CLAUDE_NATIVE_BACKEND:
        return _ReviewDeliverability(True, "", backend, tracker)
    if backend == CODEX_BACKEND:
        if tracker == TRACKER_GITHUB_ISSUES:
            return _ReviewDeliverability(True, "", backend, tracker)
        return _ReviewDeliverability(
            False,
            (
                f"REVIEW-stage backend 'codex' for client {client_cfg.name!r} can only"
                " deliver operator tracker comments on a github-issues tracker"
            ),
            backend,
            tracker,
        )
    return _ReviewDeliverability(
        False,
        f"REVIEW-stage backend {backend!r} has no operator-comment delivery path",
        backend,
        tracker,
    )


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
            is not in the client pipeline, regresses a non-blocked task, if
            allow_regress is set with no backward stage_override, or if a
            forward bypass targets stage 'impl' with no approved plan
            available locally or on the tracker (#1681).
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
            stage_override=stage_override,
            client_cfg=client_cfg,
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
            # #1794: a forward/same-stage bypass resolves any pending regress
            # in the same lock, so the per-arrival marker must not survive to
            # a later, unrelated stage entry.
            task.regressed_into_stage = None

        to_stage = task.stage
        regress_attempts = task.regress_attempts if regressed else 0

        # #1730: every successful requeue landing at REVIEW is, by construction,
        # a review-stage re-entry of a previously-parked ticket -- i.e. the
        # moment an operator send-back comment is supposed to reach the
        # reviewer. If the resolved backend cannot deliver it, degrade LOUDLY
        # and proceed: a hard-fail guard here would invert the asymmetry
        # _apply_requeue_stage already codifies (impl hard-exits on a missing
        # plan; review/finalize degrade). Emitted inline while dev_queue_lock is
        # still held, mirroring _emit_stage_change's chokepoint convention --
        # record_event takes _inbox_lock *inside* dev_queue_lock and the reverse
        # nesting never occurs, so the ordering is deadlock-safe (RFC 0008 W1).
        # Inline rather than per-caller because requeue_ticket's other
        # production caller (dev_queue/drain.py) builds no events of its own.
        if to_stage == Stage.REVIEW:
            deliverable, reason, backend, tracker = _review_reentry_deliverable(
                task, client_cfg
            )
            if not deliverable:
                record_event(
                    OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
                    {
                        "ticket_id": ticket_id,
                        "client": client_name,
                        "reason": reason,
                        "backend": backend,
                        "tracker": tracker,
                    },
                    correlation_id=ticket_id,
                )

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
