"""Dev-queue task lifecycle: status transitions, stage pointer, terminal wait.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
mutation-authority layer for a ``TicketTask``: ``transition_task_status`` (the
single status-transition primitive), the disposition/terminal-status constants,
the stage-pointer helpers (``_advance_task_pointer`` / ``_stage_regress`` /
``_raise_stage_high_water`` / ``_stamp_salvage_stage`` /
``_clear_signoff_gate``), the same-stage requeue
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
from cw.models import OrchestratorEventType, QueueItemStatus, Stage

if TYPE_CHECKING:
    from cw.models import TicketTask

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

# Disposition stamped when dispatch's REVIEW-stage routing refuses to advance a
# ticket whose sentinel reported health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
# (#1702). Distinct from SIGNOFF_GATE_DISPOSITION above: a signoff park is an
# *authorization* slot an operator `approve` clears, whereas this park is a
# *quality* signal -- the review that just ran did not vouch for its own
# coverage, so there is nothing to authorize yet.
#
# Deliberately NOT a member of HOLD_DISPOSITIONS below. That set means "parked
# pending a human/dependency, not pending a fix"; degraded review health IS
# pending a fix -- it clears by re-running review, not by an operator saying
# yes. Adding it there would also silently make it eligible for concierge's
# false-park auto-requeue recipe (which draws from the same
# _REAP_ELIGIBLE_DISPOSITIONS_BASE lineage), defeating the gate. Same treatment
# SIGNOFF_GATE_DISPOSITION already gets.
REVIEW_HEALTH_GATE_DISPOSITION = "review_health_gate"

# Disposition stamped when dispatch's Rule 5 routes a blocked sentinel whose
# blocker.reason is codex_review's CODEX_MUST_FIX_MECHANICALLY_REJECTED -- the
# review produced a MUST_FIX finding, but validation dropped it (bad anchor,
# evidence absent from the diff) before adjudication could weigh it (#1714).
#
# Unlike every other disposition in this module, this one is NOT derived from a
# (status, blocker_reason) pair by _derive_disposition/_hold_aware_disposition.
# It is stamped directly by dispatch.routing._park_must_fix_mechanically_
# rejected, Rule 5's sole reason-keyed override -- deliberately, so it can
# never resolve to AWAITING_OPERATOR_DISPOSITION (a HOLD_DISPOSITIONS member)
# by riding the OPERATOR_UNAVAILABLE_BLOCKER_REASONS path.
#
# Same set-membership treatment as REVIEW_HEALTH_GATE_DISPOSITION above, for
# the same reason: it is a *quality* signal, not an authorization slot. A
# dropped MUST_FIX is pending a fix (re-run review with the finding
# adjudicated), not pending an operator saying "proceed anyway" -- so it is
# deliberately NOT a HOLD_DISPOSITIONS member, which would also silently make
# it eligible for concierge's false-park auto-requeue and defeat the gate.
REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION = (
    "codex_must_fix_mechanically_rejected"
)

# Textually identical to cw.reconcile._shared._NEEDS_SALVAGE_REASON
# ("needs_salvage") but a SEPARATE constant, not an import of it: _shared
# imports FROM cw.dev_queue (dev_queue -> reconcile is the only cycle-safe
# direction), so lifecycle.py cannot import the reconcile-side copy without
# a cycle. Unlike AWAITING_OPERATOR_DISPOSITION/FINALIZE_GATE_HELD_DISPOSITION
# above (deliberately DISTINCT values from their dispatch.routing namesakes --
# safe to diverge), this constant's value must stay identical to
# _NEEDS_SALVAGE_REASON for the seam guard below to keep matching salvage.py's
# call; test_dev_queue.py asserts the two literals in lockstep so a future
# edit to either fails loudly instead of silently breaking the stamp.
# transition_task_status string-matches against this value to recognize the
# salvage.py LOW-path park and stamp TicketTask.salvage_no_sentinel_at
# (GitHub #1638, R2/R8/R5).
_SALVAGE_NO_SENTINEL_DISPOSITION = "needs_salvage"

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
# Canonical home (#1567): this module, not cw.reconcile.gate_recipes. Why
# here and not there: gate_recipes already imports from cw.dev_queue (for
# _approve_ticket_locked et al.), so dev_queue -> gate_recipes would be a
# cycle; gate_recipes -> dev_queue is the only cycle-safe direction. Both
# constants, _marker_version, and _plan_body_signoff_ok live here;
# gate_recipes imports all four from cw.dev_queue instead of defining its own
# copies. See #968 for the original duplication and #1567 for the unification.
_PLAN_SPEC_MARKER = "<!-- plan-spec-reviewed"
_PLAN_SOUNDNESS_MARKER = "<!-- plan-soundness-reviewed"


def _marker_version(body: str, *, marker: str) -> str | None:
    """Extract the ``<date> <vN>`` version string that follows *marker*.

    Fails closed (returns None) both when *marker* is absent from *body* and
    when its comment is never closed with ``-->`` — the caller currently
    always pre-checks marker presence, but this function defends its own
    fail-closed contract rather than depending on that discipline, since a
    future caller (or refactor) skipping the pre-check would otherwise hit an
    uncaught ``IndexError`` instead of failing closed. Substring split only —
    no regex or date parser (R3): the marker line is
    ``<!-- plan-spec-reviewed: D vN -->``, so we take everything between the
    marker and the ``-->`` close, strip the leading ``:`` and surrounding
    whitespace. The closure check specifically prevents ``str.split`` from
    silently returning the rest of *body* verbatim, which would leak raw
    plan-of-record text into the predicate_snapshot, the GATE_AUTO_APPROVED
    event payload, and the public audit comment.
    """
    if marker not in body:
        return None
    rest = body.split(marker, 1)[1]
    if "-->" not in rest:
        return None
    return rest.split("-->", 1)[0].lstrip(":").strip()


def _plan_body_signoff_ok(body: str) -> bool:
    """True iff *body* carries both signoff markers, each properly closed.

    The single shared predicate for "is this plan-of-record body reviewed" —
    composes :func:`_marker_version` (fail-closed on absent-or-unclosed
    marker) over both markers rather than a bare ``marker in body`` substring
    check, which a merely-opened, never-closed marker comment would
    incorrectly satisfy (#1567). Shared by :func:`_plan_is_reviewed` here and
    ``cw.reconcile.gate_recipes._clean_plan_snapshot``'s presence pre-check.

    Distinct from ``cw.gh._comment_has_marker``: that helper selects *which*
    GitHub comment on a ticket is the plan-of-record (matching a marker
    substring across many comments); this predicate instead validates the
    signoff state of a single already-selected body. Deliberately does not
    validate the marker's version-string *format* (e.g. that it matches a
    date/vN shape) — presence of a well-closed marker is the whole contract;
    a malformed version string inside a validly-closed marker is out of scope
    for "is this reviewed".
    """
    return (
        _marker_version(body, marker=_PLAN_SPEC_MARKER) is not None
        and _marker_version(body, marker=_PLAN_SOUNDNESS_MARKER) is not None
    )


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
        # GitHub #1638 R2/R8: the shared seam, not a 5th call site. Only
        # salvage.py's LOW path (_notify_needs_salvage) passes this disposition
        # (verified: grep -rn 'disposition=_NEEDS_SALVAGE_REASON' src/cw --
        # exactly one hit). Deliberately does NOT touch task.stage or
        # stage_high_water: unlike the four _stamp_salvage_stage sites (which
        # drive a row to a TERMINAL disposition), this park is RESPAWNABLE via
        # unblock_ticket / the concierge poison-catch-all, neither of which
        # resets task.stage -- forcing FINALIZE here would make recovery skip
        # the verification tail this field exists to flag as skipped.
        if (
            new_status is QueueItemStatus.BLOCKED_ON_USER
            and disposition == _SALVAGE_NO_SENTINEL_DISPOSITION
        ):
            task.salvage_no_sentinel_at = datetime.now(UTC)
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
    # GitHub #1162 (RFC 0011 A6): same unconditional-clear treatment for the
    # attention-digest buffer marker -- a status transition means the row's
    # SESSION_NEEDS_ATTENTION episode (if any) has ended, so any pending
    # digest buffer membership for it is stale and must not survive into the
    # next episode. This is what satisfies R9's "re-derive live state" digest
    # requirement structurally: cw.cw_operator_events._peek_flushable_digest
    # only ever sees tasks whose marker is still set.
    task.attention_digest_buffered_at = None
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
    ``_stage_regress`` (regress), ``_apply_requeue_stage``'s forward/
    same-stage tail (advance), and ``_stamp_salvage_stage`` (advance, #1629).
    Guarded on ``old_stage != new_stage`` so a same-stage requeue stays
    silent. ``direction`` is the closed enum ``"advance" | "regress"``.
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


def _stamp_salvage_stage(task: TicketTask) -> None:
    """Force ``task.stage`` to FINALIZE for a terminal salvage completion.

    Companion write for the four reconcile backstops that stamp a terminal
    ``disposition="shipped"`` without ever having walked the stage pointer
    through the normal advance/regress path (GitHub #1629). Deliberately
    does NOT call :func:`_raise_stage_high_water` (R1): leaving
    ``stage_high_water`` untouched is what lets a completed row's
    ``stage_high_water != stage`` identify "salvaged before reaching
    finalize." Deliberately does NOT route through
    :func:`_advance_task_pointer` (R2): that helper also transitions status
    to PENDING, clears ``session_id``/``stage_base_ref``, and expects to move
    exactly one pipeline position -- all wrong for a terminal write.

    Honest limit: a task salvaged after it had already reached FINALIZE has
    ``stage == stage_high_water == finalize`` already, so this call is a
    no-op (and emits no event either) -- that completion stays
    indistinguishable from a normally-routed one. Accepted for #1629; a
    distinct disposition string is the documented follow-up if full
    distinguishability is ever needed.
    """
    old_stage = task.stage
    task.stage = Stage.FINALIZE
    _emit_stage_change(task, old_stage, Stage.FINALIZE, "advance")


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
    than raising. See GitHub #968. The reviewed-check itself delegates to
    :func:`_plan_body_signoff_ok` (#1567), which fails closed on a marker that
    is present but never closed with ``-->`` — a bare substring check would
    incorrectly accept that malformed body as reviewed.
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
    return _plan_body_signoff_ok(body)


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

    Not gated by the RFC 0011 A3 force hold (#1160): the automatic gate-recipe
    reactor can never reach this function. Two independent facts combine to
    prove it, both checkable in place (not merely asserted):

    1. ``_approve_ticket_locked`` (``dev_queue/approval.py``) only calls this
       function from its own explicit
       ``if task.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:`` branch
       -- checked after task resolution and the approvability/stage guard
       clauses, but still before the ``operator_initiated``/force-hold branch
       below it is ever reached.
    2. The automatic reactor's candidates are produced by
       ``_detect_auto_approve_review`` and re-resolved by
       ``_find_blocked_task`` (both in ``cw.reconcile.gate_recipes``), and both
       filter on ``task.status == QueueItemStatus.BLOCKED_ON_USER`` -- the
       mutually exclusive status to (1)'s precondition.

    So the reactor can only ever invoke ``_approve_ticket_locked`` on a row
    whose status is ``BLOCKED_ON_USER``, which by (1) can never take the
    ``_clear_signoff_gate`` branch. Only the human ``approve_ticket`` path
    (which resolves an ``AWAITING_OPERATOR_SIGNOFF`` row) reaches here, so no
    ``operator_initiated`` threading is needed.
    """
    if task.stage == stages[-1]:
        transition_task_status(
            task, QueueItemStatus.COMPLETED, disposition=SIGNOFF_GATE_DISPOSITION
        )
    else:
        _advance_task_pointer(task, stages)
