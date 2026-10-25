"""Dev-queue task lifecycle: status transitions, stage pointer, terminal wait.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
mutation-authority layer for a ``TicketTask``: ``transition_task_status`` (the
single status-transition primitive), the terminal-status constants,
the stage-pointer helpers (``_advance_task_pointer`` / ``_stage_regress`` /
``_raise_stage_high_water`` / ``_stamp_salvage_stage`` /
``_clear_signoff_gate``), the same-stage requeue
reset, the plan-review gate (``_plan_is_reviewed``), and the terminal-wait poll
loop (``wait_for_terminal`` + its ``consume_completed_sessions`` dispatch
wrapper). The disposition vocabulary and the sentinel-derivation helpers moved
to the leaf ``dispositions`` (#2719).

Layering: this module sits just above ``storage``/``migrate``/``dispositions``
and below ``crud``. ``crud`` imports ``transition_task_status`` from here at
module level; the one back-edge — ``wait_for_terminal`` needing
``crud._find_ticket`` — is broken with a function-level deferred import (see
``wait_for_terminal``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from cw.dev_queue.dispositions import (
    _SALVAGE_NO_SENTINEL_DISPOSITION,
    SIGNOFF_GATE_DISPOSITION,
)
from cw.dev_queue.storage import load_dev_queue
from cw.events import record_event
from cw.gh import fetch_approved_plan_comment
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask

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
    ``cw.reconcile.gate_recipes._clean_plan_snapshot``'s ``plan_reviewed`` read.

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
    unproductive: bool = True,
) -> None:
    """Single authority for TicketTask status transitions. Mutates in place.

    Stamps disposition/pr_url/completed_at/blocked_reason on terminal
    transitions (COMPLETED/BLOCKED_ON_USER/FAILED/AWAITING_OPERATOR_SIGNOFF);
    clears them on PENDING/CANCELLED (requeue/cancel).  Companion field resets
    (session_id, stage_base_ref) stay at call sites.  GitHub #310, #990, #1511.

    Emits a ``task.transition`` orchestrator event on a real status change (RFC
    0008 W1, closes #978); the emit is suppressed when new_status == old_status
    so a re-assert of the same status stays silent.

    *unproductive* (GitHub #1750): whether this RUNNING exit produced no
    evidence of progress, and so should be charged against
    ``task.unproductive_attempts`` — the counter the global attempt ceiling
    reads. Defaults to ``True`` deliberately: the ~30 non-sentinel RUNNING-exit
    call sites across the codebase (crash/phantom/stalled/wedge/spawn-error
    reverts) are unproductive by construction and need no change, so only a
    caller holding positive evidence of progress passes ``unproductive=False``.
    The charge is gated on a genuine RUNNING *exit*, so a park→park move or a
    RUNNING→RUNNING re-assert never charges, and a caller that routes the same
    claim through two transitions cannot double-charge it. Callers that pass
    ``False`` are, in the same shape as disposition/pr_url/blocked_reason,
    supplying a fact this seam then stamps centrally.
    """
    old_status = task.status
    task.status = new_status
    # GitHub #1750: the single increment seam for the unproductive counter,
    # mirroring regress_attempts's v6 single-seam precedent. Computed before
    # the disposition branches below so the audit payload can report it.
    running_exit = (
        old_status == QueueItemStatus.RUNNING and new_status != QueueItemStatus.RUNNING
    )
    if running_exit and unproductive:
        task.unproductive_attempts += 1
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
    # GitHub #1713: same unconditional-clear treatment for the stale-gate
    # detection latch and its cross-reference companion -- a status
    # transition means whatever gate episode stale_gate_detected_at/
    # blocked_on_pr described (if any) has ended, so a fresh parked episode
    # always starts with a clean latch. release_stale_gated_tasks re-stamps
    # stale_gate_detected_at on its next sweep if the row is still (or newly)
    # gate-stale under ReapPolicy.SIGNAL_ONLY.
    task.stale_gate_detected_at = None
    task.blocked_on_pr = None
    # GitHub #1762: same unconditional-clear treatment for the non-terminal
    # operator advisory. The note describes the row's *current* session binding;
    # a status transition means that binding just changed (or was re-asserted
    # under a fresh disposition), so a stale note must never survive into the
    # next episode. _stamp_session_id_mismatch_advisories re-derives it on the
    # next reconcile tick if the condition still holds.
    task.advisory_note = None
    # GitHub #2324: the mid-turn usage-limit act's intent. Its own final
    # transition clears it in the same row write; any other transition means
    # someone else dispositioned the row, which ends the act.
    task.usage_limit_act = None
    # GitHub #2307: same unconditional-clear treatment for the live-writer
    # codex-orphan park's session link and its rescan backoff -- a status
    # transition means that park episode has ended (requeued, unblocked,
    # cancelled, or re-parked under a fresh disposition), so a stale link must
    # never lead cw.reconcile.codex_reparks to a session the row no longer
    # describes. _park_running_task_blocked_on_user re-stamps the link after
    # its own transition when the new park leaves the session ACTIVE.
    task.codex_orphan_session_id = None
    task.codex_orphan_rescan_next_eligible_at = None
    if old_status != new_status:
        # Why: emit inline while callers still hold dev_queue_lock; the
        # inbox lock nests inside it as a leaf (ADR-0019). RFC 0008 W1.
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
                # GitHub #1750: durable audit record for the attempt charge.
                # `unproductive_attempts` is the running total *after* this
                # transition; `unproductive_charge` is what THIS transition
                # did — True (charged), False (declined, caller had evidence),
                # or None (not a RUNNING exit, so the guard never fired). The
                # three-way split matters: None and False both leave the total
                # unchanged, but only False means a caller actively vouched
                # for progress, which is the fact an operator reconstructing a
                # park decision needs.
                "unproductive_attempts": task.unproductive_attempts,
                "unproductive_charge": unproductive if running_exit else None,
            },
            correlation_id=task.ticket_id,
        )


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
    ``_stage_regress`` (regress), ``_stamp_salvage_stage`` (advance, #1629),
    and -- via the shared :func:`_advance_stage` helper (#1286 Fix A) --
    ``_apply_requeue_stage``'s forward/same-stage tail and
    ``_apply_plan_bypass_if_available``'s auto-bypass (both advance). The
    auto-bypass caller passes the stored row found under
    ``dev_queue_lock()`` inside that function, not a caller-held ``task``
    reference. Guarded on ``old_stage != new_stage`` so a same-stage requeue
    stays silent. ``direction`` is the closed enum ``"advance" | "regress"``.
    """
    if old_stage == new_stage:
        return
    # Why: emit inline while callers still hold dev_queue_lock; the inbox
    # lock nests inside it as a leaf (ADR-0019). RFC 0008 W1.
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


def _advance_stage(task: TicketTask, stages: list[Stage], target_stage: Stage) -> Stage:
    """Advance ``task.stage`` forward, raise high-water, emit the change event.

    Shared tail for every forward stage-pointer mutation (GitHub #1286 Fix
    A): extracted from :func:`~cw.dev_queue.requeue._apply_requeue_stage`'s
    forward/same-stage tail, and reused by
    :func:`~cw.dispatch.claim._apply_plan_bypass_if_available`'s auto-bypass
    advance -- both previously near-duplicated the same
    old_stage/mutate/``_raise_stage_high_water``/``_emit_stage_change`` four
    lines. Operates on a ``task`` the caller has already loaded (and, for the
    dev-queue-backed callers, holds under :func:`dev_queue_lock` for) --
    this helper does no locking, loading, or saving of its own; the caller
    keeps that ownership. Returns the pre-mutation stage.
    """
    old_stage = task.stage
    task.stage = target_stage
    _raise_stage_high_water(task, stages, target_stage)
    _emit_stage_change(task, old_stage, target_stage, "advance")
    return old_stage


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
    Deliberately does NOT clear ``regressed_into_stage`` either (R3, GitHub
    #1794): the row is headed to a terminal disposition and will not be
    re-dispatched off this stage without a fresh regress or forward requeue,
    both of which already own that reset.

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

    Does NOT touch ``regressed_into_stage`` (GitHub #1794) — by the time any
    forward advance fires, the task's current stage was already entered via a
    dispatch spawn or a forward requeue, both of which already clear that
    marker. Clearing it here would be a no-op; *reading* it here would
    resurrect the sticky-counter false positive the field exists to avoid.
    """
    old_stage = task.stage
    idx = stages.index(task.stage)
    task.stage = stages[idx + 1]
    _raise_stage_high_water(task, stages, task.stage)
    # unproductive=False (GitHub #1750): this is the shared chokepoint for
    # EVERY forward advance (approval.py's approve path, routing.py's Rule 3
    # and stage-pointer walk), and a claim that moved the pipeline forward a
    # stage is productive by definition. Hardcoding it here rather than
    # threading a stage_changed flag through every caller means no caller
    # needs to know the counter exists.
    transition_task_status(task, QueueItemStatus.PENDING, unproductive=False)
    task.session_id = None  # R6: clear session_id on advance
    task.stage_base_ref = None  # cleared so next spawn stamps fresh ref
    _emit_stage_change(task, old_stage, task.stage, "advance")


def _clear_review_approval(task: TicketTask) -> None:
    """Drop *task*'s v48 review-approval record (#2767); all three go together."""
    task.review_approved_at = None
    task.review_approver_kind = None
    task.review_approved_sha = None


def _stage_regress(
    task: TicketTask, target_stage: Stage, *, merge_conflict_detected: bool = False
) -> None:
    """Regress task to a prior pipeline stage for self-heal.

    Mutates task in-place: sets stage to target_stage, increments
    regress_attempts, reverts status to PENDING, and clears session anchors.
    worktree_path is preserved so the next impl session resumes the branch.
    Caller is responsible for stage selection and regress-cap enforcement.
    See GitHub #770. stage_high_water is deliberately NOT touched here --
    it is monotonic and must never be lowered by a regress (GitHub #1361).

    Also stamps ``regressed_into_stage = target_stage`` (GitHub #1794) -- a
    per-arrival marker distinct from the cumulative ``regress_attempts``,
    consumed and cleared by the next dispatch spawn (dispatch/claim.py) after
    it has been written into the worker's ``queue_metadata``.

    Also clears ``plan_approved_at`` and its v36 companion
    ``plan_approved_fingerprint`` when *target_stage* is ``Stage.PLAN``
    (GitHub #2102) -- a regress into the plan stage means re-plan, and the
    approval the operator gave the previous plan must not carry over.

    Also clears ``must_fix_override`` when *target_stage* is ``Stage.REVIEW``
    (GitHub #2205) -- the re-review produces a new verdict, and an override
    bound to the old one must not ship the new one.

    Also clears the v48 review-approval record (``review_approved_at``,
    ``review_approver_kind``, ``review_approved_sha``, GitHub #2767) for every
    target -- the work the approval was bound to is about to be redone. The
    durable trace of who approved which head is the
    ``dispatch.scope_routing_decision`` event ``approve`` emitted.

    Also stamps ``pending_operator_comment = True`` (GitHub #1730) -- the
    sibling per-arrival marker saying this re-entry may carry an operator
    send-back the reviewer must treat as binding. Stamped unconditionally here,
    exactly like ``regressed_into_stage``; the stage gate lives at the
    *consumption* site (``dispatch/claim.py`` clears it only at a REVIEW-stage
    spawn), not at this shared stamp point, because Rule 5a's self-heal
    regresses to IMPL and must not consume a marker meant for REVIEW.

    ``merge_conflict_detected`` (GitHub #2421) is the caller's live
    ``merge_in_progress()`` measurement, stamped onto
    ``finalize_regress_merge_conflict_detected`` under the same FINALIZE-origin
    gate as ``finalize_regress_branch_head``. Measured by the caller so this
    function stays pure mutation, with no git I/O.
    """
    old_stage = task.stage
    task.stage = target_stage
    task.regress_attempts += 1
    task.regressed_into_stage = target_stage
    # Shared seam (#1717/#1730 unified re-entry contract, GitHub issue
    # comments posted 2026-08-10): both tickets stamp per-regress-arrival
    # markers here, immediately before stage_base_ref is cleared below.
    # Each field is independently gated by its own precondition and consumed
    # exactly once by its own REVIEW-re-entry reader; neither write may
    # clobber the other's field. #1717 owns finalize_regress_branch_head
    # (FINALIZE-origin only -- gated below, and it must be read BEFORE
    # stage_base_ref is cleared). #1730 owns pending_operator_comment, which
    # follows the identical shape with its gate at the *consumption* site
    # instead: stamped on every regress origin here, cleared only at a
    # REVIEW-stage spawn (dispatch/claim.py), because Rule 5a's self-heal
    # regresses to IMPL and must not consume a marker meant for REVIEW.
    # Both fields have now landed; the mandatory compose test -- a re-entry
    # that is simultaneously a same-branch-head repeat AND carries a pending
    # operator send-back -- is #1730's obligation as the ticket landing
    # second, and lives in tests/test_dispatch.py's
    # TestUnifiedReentryContractCompose per the paired resolution comment.
    task.pending_operator_comment = True
    if old_stage == Stage.FINALIZE:
        task.finalize_regress_branch_head = task.stage_base_ref
        task.finalize_regress_merge_conflict_detected = merge_conflict_detected
    # A deliberate regress INTO the plan stage is a "re-plan": the operator's
    # earlier plan approval (schema v35, dev_queue/approval.py) was given for
    # text that is about to be superseded, so it must not auto-clear the next
    # Large-scope Checkpoint 1 (see GitHub #2102). Same-stage PLAN requeues
    # never pass through here and keep the approval on purpose -- an
    # approved draft re-parked for ambiguities still carries its approval.
    if target_stage == Stage.PLAN:
        task.plan_approved_at = None
        task.plan_approved_fingerprint = None
    # A regress INTO review means a re-review, which supersedes the verdict the
    # operator's MUST_FIX override was bound to (#2205).
    if target_stage == Stage.REVIEW:
        task.must_fix_override = None
    # Any regress re-runs work the review approval was bound to, so it is
    # revoked for every target (#2767); a re-review re-stamps it at approve.
    _clear_review_approval(task)
    # unproductive=False (GitHub #1750): the shared chokepoint for every
    # regress (operator `--regress` via requeue.py, routing.py's Rule 5a
    # FINALIZE self-heal). A deliberate backward move is a pipeline-stage
    # change, not a wasted claim — and regress_attempts already bounds it via
    # its own independent cap, so charging the global ceiling here would
    # double-bound the same event.
    transition_task_status(task, QueueItemStatus.PENDING, unproductive=False)
    task.session_id = None
    task.stage_base_ref = None
    _emit_stage_change(task, old_stage, target_stage, "regress")


def _tracker_allows_github_fetch(client_cfg: ClientConfig | None) -> bool:
    """True unless *client_cfg*'s tracker is positively known to be non-GitHub.

    :func:`fetch_approved_plan_comment` is GitHub-only (``gh issue view``):
    called against a Linear-tracked ticket id it fails and returns ``None``
    after paying the ``gh`` subprocess cost, which masqueraded as "the tracker
    was checked and had no reviewed plan" (#1906). Same fail-open polarity
    as ``requeue._impl_bypass_plan_available``: an unresolvable tracker
    (``None`` client, or no ``.claude/project-config.yaml``) still attempts
    the GitHub fetch exactly as before; only a *known* non-GitHub tracker
    skips it.
    """
    if client_cfg is None:
        return True
    tracker = resolve_tracker(client_cfg.workspace_path)
    return tracker is None or tracker == TRACKER_GITHUB_ISSUES


def _local_plan_path(task: TicketTask, client_cfg: ClientConfig | None) -> Path | None:
    """Resolve the on-disk ``.cw/plan.md`` for *task*, or None.

    The stamped-wins-then-branch-derived-fallback rule lives in
    :func:`cw.worktree.resolve_task_worktree`; #2123 extracted it there after a
    second consumer (``dispatch.review_gates``) needed the same resolution and
    a duplicate copy read only the never-stamped ``task.worktree_path``.
    """
    wt_path = resolve_task_worktree(task, client_cfg)
    if wt_path is None:
        return None
    return wt_path / ".cw" / "plan.md"


def _local_plan_body(task: TicketTask, client_cfg: ClientConfig | None) -> str | None:
    """Return the worktree's ``.cw/plan.md`` text for *task*, or None.

    A missing worktree, a missing file, or a read/decode failure between
    ``.exists()`` and ``.read_text()`` all degrade to ``None`` (fail-closed)
    rather than raising -- an unhandled exception here would abort the
    caller's whole reconcile tick or approve call. ``UnicodeDecodeError`` is
    not an ``OSError`` subclass, so it is caught explicitly alongside it.
    """
    plan_path = _local_plan_path(task, client_cfg)
    if plan_path is None or not plan_path.exists():
        return None
    try:
        return plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _plan_is_reviewed(task: TicketTask, client_cfg: ClientConfig | None) -> bool:
    """True iff the plan-of-record for *task* carries both signoff markers.

    Mirrors ``gate_recipes._plan_of_record_body``'s tracker-first,
    ``.cw/plan.md``-fallback fetch shape: the tracker (GitHub issue comments)
    is checked first via :func:`fetch_approved_plan_comment` -- skipped when
    :func:`_tracker_allows_github_fetch` says the client's tracker is
    positively non-GitHub -- then the worktree's ``.cw/plan.md`` resolved by
    :func:`_local_plan_body`. Any miss degrades to "not reviewed"
    (fail-closed) rather than raising. See GitHub #968. The reviewed-check
    itself delegates to :func:`_plan_body_signoff_ok` (#1567), which fails
    closed on a marker that is present but never closed with ``-->`` -- a
    bare substring check would incorrectly accept that malformed body as
    reviewed.
    """
    body = None
    if _tracker_allows_github_fetch(client_cfg):
        body = fetch_approved_plan_comment(task.ticket_id)
    if body is None:
        body = _local_plan_body(task, client_cfg)
    return body is not None and _plan_body_signoff_ok(body)


def _reset_for_same_stage_requeue(task: TicketTask) -> None:
    """Reset session anchors and revert *task* to PENDING for a non-advancing
    requeue at its current stage.

    Shared 3-line mutation extracted from ``requeue_ticket``'s forward/same-
    stage tail: reused here for the #968 unreviewed-plan re-park path.
    ``regress_attempts`` is intentionally NOT touched -- ``requeue_ticket``
    resets it itself immediately after calling this helper; the approve path
    never regresses, so it has no analogous reset to make.

    A REVIEW-stage reset also clears ``must_fix_override`` (#2205) and the
    review-approval record (#2767): both are bound to the prior review round.
    A FINALIZE re-run keeps the record, which Step 4.0 re-checks.
    """
    # A same-stage REVIEW requeue starts a new review round. An override is
    # bound to the prior verdict and must not survive that re-entry (#2205),
    # and neither may an approval of the prior round (#2767).
    if task.stage == Stage.REVIEW:
        task.must_fix_override = None
        _clear_review_approval(task)
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
