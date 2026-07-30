"""Dev-queue task lifecycle: status transitions, stage pointer, terminal wait.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
mutation-authority layer for a ``TicketTask``: ``transition_task_status`` (the
single status-transition primitive), the disposition/terminal-status constants,
the stage-pointer helpers (``_advance_task_pointer`` / ``_stage_regress`` /
``_raise_stage_high_water`` / ``_clear_signoff_gate``), the same-stage requeue
reset, the plan-review gate (``_plan_is_reviewed``), and the terminal-wait poll
loop (``wait_for_terminal`` + its ``consume_completed_sessions`` dispatch
wrapper).

Layering: this module sits just above ``storage``/``migrate`` and below
``crud``. ``crud`` imports ``transition_task_status`` from here at module level;
the one back-edge — ``wait_for_terminal`` needing ``crud._find_ticket`` — is
broken with a function-level deferred import (see ``wait_for_terminal``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import (
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    AutoDevResult,
    BlockedResult,
)
from cw.dev_queue.storage import load_dev_queue
from cw.events import record_event
from cw.gh import fetch_approved_plan_comment
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from cw.models import Stage, TicketTask

_WAIT_POLL_INTERVAL: int = 5

_TERMINAL_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)

# Statuses that should stamp disposition/pr_url/completed_at on the task.
_TERMINAL_DISPOSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.COMPLETED,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.FAILED,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)

# Disposition stamped when a ticket is parked AWAITING_OPERATOR_SIGNOFF by
# either dispatch's staged-decision routing (RFC 0007 Phase 3, dispatch.py) or
# approve_ticket's own REVIEW-stage re-check below. Imported by dispatch.py via
# a function-level import to break the dev_queue<->dispatch circularity
# (mirrors reconcile/_shared.py's precedent for the same import shape).
SIGNOFF_GATE_DISPOSITION = "signoff_gate"

# Disposition stamped when a park is a *hold* -- "we could not reach the
# operator or a dependency", not "this leg is broken" (RFC 0011 A1, #1254).
# Textually distinct from dispatch.routing._AWAITING_OPERATOR_REASON
# ("awaiting_operator_availability"): that one is a SESSION_NEEDS_ATTENTION
# paused_status string, this one classifies TicketTask.disposition. Different
# namespaces -- do not confuse them.
AWAITING_OPERATOR_DISPOSITION = "awaiting_operator"

# Disposition stamped when a park is a *proactive* hold -- "an operator asked
# this ticket to stop before an unattended finalize", not "we could not reach
# anyone" (RFC 0011 A3, #1160). Textually distinct from
# dispatch.routing._FINALIZE_HOLD_REASON ("finalize_hold"): that one is a
# SESSION_NEEDS_ATTENTION paused_status string, this one classifies
# TicketTask.disposition. Different namespaces -- do not confuse them. Released
# by an explicit ``cw dev-queue approve``.
FINALIZE_GATE_HELD_DISPOSITION = "finalize_gate_held"

# The shared hold-disposition namespace: the set of TicketTask.disposition
# values that mean "parked pending a human/dependency, not pending a fix". This
# is the *selection contract* consumers (attention layer, and later A4
# auto-resume) match on, so they do not have to re-derive the hold class from
# blocker reasons themselves. Spans RFC 0011 A1 (the availability park) and A3
# (the proactive force hold, #1160) -- A3 extended this frozenset in place
# rather than adding a parallel set. Note that not every consumer wants both
# members: `drain`'s DRAIN_DISPOSITIONS deliberately selects the A1 subset only
# (RFC 0011 A4 R11), so a force hold is never batch-released.
HOLD_DISPOSITIONS: frozenset[str] = frozenset(
    {AWAITING_OPERATOR_DISPOSITION, FINALIZE_GATE_HELD_DISPOSITION}
)

# Statuses that should clear disposition/pr_url/completed_at (requeue/cancel).
_RESET_DISPOSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.PENDING,
        QueueItemStatus.CANCELLED,
    ]
)

# The two signoff markers auto-dev-plan appends to the plan-of-record body.
# Local copy of cw.reconcile.gate_recipes._PLAN_SPEC_MARKER /
# _PLAN_SOUNDNESS_MARKER -- importing gate_recipes from here would create an
# import cycle (gate_recipes already imports from dev_queue), so this module
# keeps its own copy. Keep the two pairs in sync; drift is caught by
# test_approve_plan_reviewed_marker_constants_match_gate_recipes. See #968.
_PLAN_SPEC_MARKER = "<!-- plan-spec-reviewed"
_PLAN_SOUNDNESS_MARKER = "<!-- plan-soundness-reviewed"


def transition_task_status(
    task: TicketTask,
    new_status: QueueItemStatus,
    *,
    disposition: str | None = None,
    pr_url: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    """Single authority for TicketTask status transitions. Mutates in place.

    Stamps disposition/pr_url/completed_at/blocked_reason on terminal
    transitions (COMPLETED/BLOCKED_ON_USER/FAILED/AWAITING_OPERATOR_SIGNOFF);
    clears them on PENDING/CANCELLED (requeue/cancel).  Companion field resets
    (session_id, stage_base_ref) stay at call sites.  GitHub #310, #990, #1511.

    Emits a ``task.transition`` orchestrator event on a real status change (RFC
    0008 W1, closes #978); the emit is suppressed when new_status == old_status
    so a re-assert of the same status stays silent.
    """
    old_status = task.status
    task.status = new_status
    if new_status in _TERMINAL_DISPOSITION_STATUSES:
        task.disposition = disposition
        task.pr_url = pr_url
        task.completed_at = datetime.now(UTC)
        task.blocked_reason = blocked_reason
    elif new_status in _RESET_DISPOSITION_STATUSES:
        task.disposition = None
        task.pr_url = None
        task.completed_at = None
        task.blocked_reason = None
    # RFC 0008 capstone (#1015, Q5): unconditional clear-site for the
    # durable-escalation-latch fields. transition_task_status is the single
    # authority for every status/disposition mutation on a TicketTask (see
    # docstring above), so clearing here — on every call, regardless of which
    # branch above fired — covers approve_ticket, requeue_ticket,
    # cancel_ticket, unblock_ticket, and _advance_task_pointer/_stage_regress
    # all at once. This is deliberately NOT gated on old_status != new_status:
    # a re-park to the same status (e.g. a fresh BLOCKED_ON_USER disposition
    # overwriting a stale one) must also start the latch clean, and clearing
    # an already-None pair is a no-op. cw.reconcile.escalation re-stamps
    # escalation_parked_at on the next sweep tick if the row is still (or
    # newly) in the escalation-eligible set.
    task.escalation_parked_at = None
    task.escalation_fired_at = None
    # RFC 0009 P1+P2 (#1065): same unconditional-clear treatment for the
    # gate-recipe failure latch — a fresh episode (any status transition,
    # including a same-status re-park by a new review session) always starts
    # with a clean latch. See cw.reconcile.gate_recipes for what stamps it.
    task.gate_recipe_failed_at = None
    if old_status != new_status:
        # Why: emit inline while callers still hold dev_queue_lock. record_event
        # takes the events-inbox lock (_inbox_lock) *inside* dev_queue_lock; the
        # reverse nesting never occurs (no path takes _inbox_lock then
        # dev_queue_lock), so this ordering is deadlock-safe. RFC 0008 W1.
        record_event(
            OrchestratorEventType.TASK_TRANSITION,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "lane": task.lane,
                "stage": task.stage,
                "old_status": old_status,
                "new_status": new_status,
                "disposition": task.disposition,
                "session_id": task.session_id,
                "pr_url": task.pr_url,
                "blocked_reason": task.blocked_reason,
            },
            correlation_id=task.ticket_id,
        )


_VERBATIM_DISPOSITION_STATUSES: frozenset[str] = (
    STAGE_SUCCESS_STATUSES
    | frozenset({"no_op", "merge_pending"})
    | STAGE_FAILURE_STATUSES
    | PAUSED_FOR_USER_INPUT_STATUSES
)


def _derive_disposition(status: str | None) -> str | None:
    """Map an AutoDevResult status to the task-level disposition string.

    Used alongside transition_task_status at terminal call sites to record
    the sentinel status verbatim (for operator-visible batch health).  GitHub #310.
    """
    if status is None:
        return "abandoned"
    if status in _VERBATIM_DISPOSITION_STATUSES:
        return status
    return "abandoned"


def _hold_aware_disposition(
    status: str | None, blocker_reason: str | None
) -> str | None:
    """Like :func:`_derive_disposition`, but classifies hold-class parks first.

    A strict superset of ``_derive_disposition``: when *blocker_reason* is an
    operator-unavailability reason (RFC 0011 A1), the park is a *hold* and gets
    the shared ``AWAITING_OPERATOR_DISPOSITION`` instead of the verbatim status.
    Every other input falls straight through to ``_derive_disposition``, so this
    is a drop-in replacement at every terminal call site.

    Complementary to ``blocked_reason``, not duplicative of it: ``blocked_reason``
    records the verbatim per-park diagnostic (which reason, for the operator to
    read), while ``disposition`` / ``HOLD_DISPOSITIONS`` is the *selection*
    contract consumers match on to find every hold-class park at once.
    """
    if blocker_reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS:
        return AWAITING_OPERATOR_DISPOSITION
    return _derive_disposition(status)


def _result_blocker_reason(result: AutoDevResult | BlockedResult) -> str | None:
    """Extract ``result.blocker.reason``, or ``None`` if *result* has no blocker.

    Single accessor for the six reconcile call sites that feed
    ``_hold_aware_disposition``'s *blocker_reason* argument from a validated
    ``AutoDevResult``/``BlockedResult`` (RFC 0011 A1, #1254). ``dispatch.routing``'s
    call site extracts from a raw ``last_result: dict`` instead and stays
    separate -- its input is unvalidated, not one of these two model types.
    """
    return result.blocker.reason if result.blocker else None


def _extract_pr_url(last_result: dict[str, object] | None) -> str | None:
    """Extract the PR URL from an AutoDevResult dict, or None if absent."""
    if last_result is None:
        return None
    pr = last_result.get("pr")
    if isinstance(pr, dict):
        url = pr.get("url")
        return str(url) if url is not None else None
    return None


def consume_completed_sessions() -> int:
    """Thin wrapper around dispatch.consume_completed_sessions.

    Exists as a named module-level function so that tests can monkeypatch
    ``cw.dev_queue.lifecycle.consume_completed_sessions`` without depending on a
    module-level circular import.  The real import is deferred to call time
    to break the dev_queue ↔ dispatch circular dependency.
    """
    from cw.dispatch import consume_completed_sessions as _impl

    return _impl()


def wait_for_terminal(
    ticket_id: str,
    client: str,
    *,
    timeout: float,
    poll_interval: float = _WAIT_POLL_INTERVAL,
) -> TicketTask:
    """Block until the given ticket reaches a terminal status.

    Calls consume_completed_sessions() once per poll tick before reading the
    queue, so it works whether or not a dispatch loop is active.

    # Why: before #471 merges, TIMED_OUT-but-PR-merged tickets may stay PENDING;
    # wait will then hit --timeout (exit 124) rather than returning COMPLETED.

    Terminal statuses: COMPLETED, FAILED, CANCELLED, BLOCKED_ON_USER,
    AWAITING_OPERATOR_SIGNOFF.
    Raises CwError if the ticket is not found.
    Raises TimeoutError if *timeout* seconds elapse before a terminal status.
    """
    from cw.dev_queue.crud import _find_ticket  # break crud<->lifecycle cycle:
    # crud.py imports lifecycle.transition_task_status at module level, so
    # lifecycle's reach back into crud for _find_ticket must be deferred. #1318.

    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client)
    if task.status in _TERMINAL_STATUSES:
        return task

    deadline = time.monotonic() + timeout
    while True:
        consume_completed_sessions()
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client)
        if task.status in _TERMINAL_STATUSES:
            return task
        if time.monotonic() >= deadline:
            raise TimeoutError
        time.sleep(poll_interval)


def _emit_stage_change(
    task: TicketTask,
    old_stage: Stage,
    new_stage: Stage,
    direction: Literal["advance", "regress"],
) -> None:
    """Emit a ``task.stage_changed`` event for a single real stage move.

    Single shared chokepoint for every stage-pointer mutation (RFC 0008 W1,
    closes #978): called from ``_advance_task_pointer`` (advance),
    ``_stage_regress`` (regress), and ``_apply_requeue_stage``'s forward/
    same-stage tail (advance). Guarded on ``old_stage != new_stage`` so a
    same-stage requeue stays silent. ``direction`` is the closed enum
    ``"advance" | "regress"``.
    """
    if old_stage == new_stage:
        return
    # Why: emit inline while callers still hold dev_queue_lock. record_event
    # takes the events-inbox lock (_inbox_lock) *inside* dev_queue_lock; the
    # reverse nesting never occurs (no path takes _inbox_lock then
    # dev_queue_lock), so this ordering is deadlock-safe. RFC 0008 W1.
    record_event(
        OrchestratorEventType.TASK_STAGE_CHANGED,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "old_stage": old_stage,
            "new_stage": new_stage,
            "direction": direction,
        },
        correlation_id=task.ticket_id,
    )


def _raise_stage_high_water(
    task: TicketTask, stages: list[Stage], new_stage: Stage
) -> None:
    """Raise ``task.stage_high_water`` to ``new_stage`` iff that's further along
    the pipeline than the current value (GitHub #1361).

    Compares via ``stages.index()`` -- pipeline order -- never StrEnum ``<``/
    ``>``, whose lexicographic string-value ordering does NOT match pipeline
    order (e.g. "finalize" < "harden" lexicographically). Degrades gracefully
    (stamps ``new_stage`` outright) if the existing ``stage_high_water`` value
    is not a member of the current ``stages`` list -- e.g. seeded from a stage
    a since-reconfigured client pipeline no longer includes -- rather than
    raising on a bare ``.index()`` call.
    """
    current = task.stage_high_water
    current_idx = stages.index(current) if current in stages else -1
    if stages.index(new_stage) > current_idx:
        task.stage_high_water = new_stage


def _advance_task_pointer(task: TicketTask, stages: list[Stage]) -> None:
    """Advance task to the next pipeline stage (no status precondition check).

    Mutates task in-place. The caller is responsible for any precondition checks.
    Does NOT check current status — approve path calls this directly;
    _stage_advance retains its RUNNING assert and calls this after.
    """
    old_stage = task.stage
    idx = stages.index(task.stage)
    task.stage = stages[idx + 1]
    _raise_stage_high_water(task, stages, task.stage)
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None  # R6: clear session_id on advance
    task.stage_base_ref = None  # cleared so next spawn stamps fresh ref
    _emit_stage_change(task, old_stage, task.stage, "advance")


def _stage_regress(task: TicketTask, target_stage: Stage) -> None:
    """Regress task to a prior pipeline stage for self-heal.

    Mutates task in-place: sets stage to target_stage, increments
    regress_attempts, reverts status to PENDING, and clears session anchors.
    worktree_path is preserved so the next impl session resumes the branch.
    Caller is responsible for stage selection and regress-cap enforcement.
    See GitHub #770. stage_high_water is deliberately NOT touched here --
    it is monotonic and must never be lowered by a regress (GitHub #1361).
    """
    old_stage = task.stage
    task.stage = target_stage
    task.regress_attempts += 1
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None
    task.stage_base_ref = None
    _emit_stage_change(task, old_stage, target_stage, "regress")


def _plan_is_reviewed(task: TicketTask) -> bool:
    """True iff the plan-of-record for *task* carries both signoff markers.

    Mirrors ``gate_recipes._plan_of_record_body``'s tracker-first,
    ``.cw/plan.md``-fallback fetch shape: the tracker (GitHub issue comments)
    is checked first via :func:`fetch_approved_plan_comment`, then the
    worktree's ``.cw/plan.md`` if the tracker read returns ``None``. A row
    with no materialized worktree, or a read failure between ``.exists()``
    and ``.read_text()``, degrades to "not reviewed" (fail-closed) rather
    than raising. See GitHub #968.
    """
    body = fetch_approved_plan_comment(task.ticket_id)
    if body is None:
        if task.worktree_path is None:
            return False
        plan_path = task.worktree_path / ".cw" / "plan.md"
        if not plan_path.exists():
            return False
        try:
            body = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
    return _PLAN_SPEC_MARKER in body and _PLAN_SOUNDNESS_MARKER in body


def _reset_for_same_stage_requeue(task: TicketTask) -> None:
    """Reset session anchors and revert *task* to PENDING for a non-advancing
    requeue at its current stage.

    Shared 3-line mutation extracted from ``requeue_ticket``'s forward/same-
    stage tail: reused here for the #968 unreviewed-plan re-park path.
    ``regress_attempts`` is intentionally NOT touched -- ``requeue_ticket``
    resets it itself immediately after calling this helper; the approve path
    never regresses, so it has no analogous reset to make.
    """
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None
    task.stage_base_ref = None


def _clear_signoff_gate(task: TicketTask, stages: list[Stage]) -> None:
    """Clear an operator-signoff gate parked on *task*, advancing the pipeline.

    The gate was parked *before* the terminal/non-terminal advance decision
    was made (dispatch's ``_stage_advance_unchecked`` was skipped in favor of
    the park), so this helper makes that decision now: complete at the
    pipeline's terminal stage, otherwise advance the stage pointer -- mirrors
    ``_stage_advance_unchecked``'s branch shape. Mutates task in place.
    See GitHub #990.
    """
    if task.stage == stages[-1]:
        transition_task_status(
            task, QueueItemStatus.COMPLETED, disposition=SIGNOFF_GATE_DISPOSITION
        )
    else:
        _advance_task_pointer(task, stages)
