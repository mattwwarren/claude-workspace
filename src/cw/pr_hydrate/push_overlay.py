"""Webhook push-event overlay onto a persisted ``PrState`` (#930).

Part of the ``cw.pr_hydrate`` package (#2999). The entry point,
``observe_pushed_event``, stays in the package ``__init__``, where the
suite patches ``apply_pr_state_observation``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.events import record_event
from cw.gh import _GH_PR_STATE_MERGED
from cw.models import OrchestratorEventType, PrState
from cw.pr_hydrate.attention import _compute_attention_state
from cw.pr_hydrate.repo_slug import _parse_pr_url
from cw.pr_hydrate.transitions import _TERMINAL_PR_STATES

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask

# Wire-level review_decision value for a comment-only review (#930). Not a
# merge-gate signal -- see the "Why" note on _emit_commented_review below.
_REVIEW_DECISION_COMMENTED = "COMMENTED"


def _resolve_task_by_pr_ref(
    store: DevQueueStore, *, repo: str, pr_number: int
) -> TicketTask | None:
    """Return the dev-queue task whose ``pr_url`` matches ``(repo, pr_number)``.

    Linear scan reusing ``_parse_pr_url`` — no separate index. A ``(repo,
    pr_number)`` with no matching task is not an error (untracked PR); the
    caller no-ops.
    """
    for task in store.tasks:
        if _parse_pr_url(task.pr_url or "") == (repo, pr_number):
            return task
    return None


def _overlay_push_observation(
    old: PrState | None, *, event_type: OrchestratorEventType, payload: dict[str, Any]
) -> PrState:
    """Build the overlay ``PrState`` for one pushed webhook event (#930).

    Starts from *old* (or a fresh ``PrState()`` baseline when there is no
    prior hydration) and overlays only the field(s) implied by *event_type*,
    refreshing ``hydrated_at``. A missing or malformed payload key is a silent
    no-op for that field — the webhook handler must never reject a request
    for an unexpected payload shape, it just skips the mutation and leaves the
    prior value (or the fresh-baseline default) in place.

    Called from inside ``apply_pr_state_observation``'s ``dev_queue_lock()``
    with the freshly-locked task state as *old* — never a pre-lock snapshot —
    so this overlay can't clobber a concurrent writer (#930 fix).

    After the field(s) implied by *event_type* are overlaid, ``attention_state``
    is always recomputed (#1196) — mirroring the poll path (``_derive_pr_state``),
    which always recomputes it via ``_compute_attention_state``. Terminal states
    (MERGED/CLOSED) short-circuit straight to None; otherwise the ladder is
    recomputed from the (possibly just-overlaid) ci_ok/merge_state_status/
    review_decision plus ``is_draft``/``reviewer_count``/``pending_count``,
    which are only ever carried forward from *base* — no wire payload ever
    carries them, so they are never themselves overlaid.

    ``has_blocking_comment_review`` (#1195) is not a ``PrState`` field and no
    webhook payload carries it, so this recompute can't re-fetch ``comments``
    to re-derive it directly. It infers whether the signal was in effect
    instead: ``changes_requested`` is produced by exactly two rows (2b or 3)
    -- KEEP THIS IN SYNC with ``_compute_attention_state``'s row table; a new
    row that also yields ``changes_requested`` would silently break this
    inference -- and row 3 requires ``review_decision == CHANGES_REQUESTED``,
    so if *base*'s ``attention_state`` was ``changes_requested`` while its
    ``review_decision`` was NOT itself ``CHANGES_REQUESTED``, row 2b must have
    been the cause, and that carries forward into this recompute. This
    recovers the common case: a comment-driven ``changes_requested`` no
    longer silently reverts on an unrelated push event mid-interval.

    Narrower residual gap (accepted, same category as the original R4 cost):
    if the comment signal was present but *masked* by a higher-precedence row
    at the prior poll (row 1, ``merge_blocked``), *base*.attention_state reads
    ``merge_blocked``, not ``changes_requested``, so this inference can't
    recover it -- a later push that clears just that masking condition
    (``PR_MERGEABLE``) still reverts to whatever the base ladder computes.
    Only the next full poll (``_derive_pr_state``, which re-fetches
    ``comments``) can re-derive it. Row 2 (``ci_failing``) masking is
    unreachable via any current push event (nothing overlays ``ci_ok`` back
    to ``True``). Fully closing this residual requires persisting
    ``has_blocking_comment_review`` as a ``PrState`` field, which the
    operator's R2 decision explicitly forbids. See
    ``TestOverlayPushObservation::test_overlay_recompute_preserves_blocking_comment_signal``
    and ``test_overlay_recompute_masked_row_gap_reverts_on_clearing_push``.
    """
    base = old if old is not None else PrState()
    updates: dict[str, Any] = {"hydrated_at": datetime.now(UTC)}
    if event_type == OrchestratorEventType.PR_MERGED:
        updates["state"] = _GH_PR_STATE_MERGED
    elif event_type == OrchestratorEventType.PR_CI_FAILED:
        updates["ci_ok"] = False
        failing_checks = payload.get("failing_checks")
        if isinstance(failing_checks, list):
            updates["failing_checks"] = [str(f) for f in failing_checks]
    elif event_type == OrchestratorEventType.PR_REVIEW_RECEIVED:
        review_decision = payload.get("review_decision")
        if review_decision is not None:
            updates["review_decision"] = str(review_decision)
    elif event_type == OrchestratorEventType.PR_MERGEABLE:
        merge_state_status = payload.get("merge_state_status")
        if merge_state_status is not None:
            updates["merge_state_status"] = str(merge_state_status)
    merged_state = updates.get("state", base.state)
    if merged_state in _TERMINAL_PR_STATES:
        updates["attention_state"] = None
    else:
        had_blocking_comment_review = (
            base.attention_state == "changes_requested"
            and base.review_decision != "CHANGES_REQUESTED"
        )
        updates["attention_state"] = _compute_attention_state(
            ci_ok=updates.get("ci_ok", base.ci_ok),
            pending_count=base.pending_count,
            merge_state_status=updates.get(
                "merge_state_status", base.merge_state_status
            ),
            review_decision=updates.get("review_decision", base.review_decision),
            is_draft=base.is_draft,
            reviewer_count=base.reviewer_count,
            has_blocking_comment_review=had_blocking_comment_review,
        )
    return base.model_copy(update=updates)


def _emit_commented_review(*, repo: str, pr_number: int, task: TicketTask) -> None:
    """Emit ``pr.review_received`` for a COMMENTED review, unconditionally.

    Why (#930 operator correction #2): COMMENTED reviews are not a
    merge-gate signal, so they never mutate PrState (only APPROVED/
    CHANGES_REQUESTED do) -- but the operator still wants an event emitted
    for every COMMENTED webhook delivery, INCLUDING duplicate/redelivered
    ones. There is no PrState field change to compare for COMMENTED, so
    apply_pr_state_observation's diff-based dedup can't apply here (and
    would wrongly suppress it) -- this path bypasses it entirely and always
    emits.
    """
    record_event(
        OrchestratorEventType.PR_REVIEW_RECEIVED,
        {
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": task.ticket_id,
            "client": task.client,
            "review_decision": _REVIEW_DECISION_COMMENTED,
        },
        correlation_id=task.ticket_id,
    )
