"""Pure gate-recipe predicates: which pending approval gates need a person.

Split out of :mod:`cw.reconcile.gate_recipes` so the predicate each recipe
fires on lives in one place with no I/O. Every function here reads only a
sentinel dict (``session.last_result``) and a :class:`~cw.models.TicketTask`
row. The tracker read, the event emission and the queue mutation stay in
``gate_recipes``.

The policy these predicates encode: **a ticket's size alone never needs a
person.** The Large tier (more than 10 files or more than 500 lines) classifies
size, not scope; the ticket the operator wrote is what authorizes the work. A
pending gate is released automatically unless a predicate names a reason a
person is actually needed:

- the plan or diff touches a forbidden area;
- the operator set ``scope_hint: large`` on the row ("gate this");
- the row is not at the stage the gate belongs to (an earlier-stage report);
- review only: the health recommendation is not PROCEED, or no reviewer ran;
- plan only: the draft carries no valid fingerprint to bind the approval to,
  or the row already holds an approval for this exact draft (the worker
  re-parked a plan that was already released, so a person must look).

Scope growth is detected elsewhere, and each detector parks under its own
reason that no recipe releases: the plan stage's ambiguity scan and Product
Manager Reviewer, ``plan_scope_drift``, and the codex fix-loop fence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.plan_fingerprint import PLAN_DRAFT_FINGERPRINT_KEY, is_plan_draft_fingerprint

if TYPE_CHECKING:
    from cw.models import Stage, TicketTask

# The only sentinel status the review recipe fires on. A row whose owning
# session's last_result is not at this gate is never a candidate.
_REVIEW_PENDING_APPROVAL = "review_pending_approval"
# The single health recommendation the clean-review predicate accepts.
_RECOMMENDATION_PROCEED = "PROCEED"
# The only sentinel status the plan recipe fires on.
_PLAN_PENDING_APPROVAL = "plan_pending_approval"

# predicate_snapshot dict keys (R3) — named once so the producer
# (_clean_plan_snapshot) and consumer (_post_auto_adopt_comment) can't drift
# via a typo'd string literal at one site only.
_SNAPSHOT_KEY_SPEC = "plan_spec_reviewed"
_SNAPSHOT_KEY_SOUNDNESS = "plan_soundness_reviewed"
_SNAPSHOT_KEY_REVIEWED = "plan_reviewed"
_SNAPSHOT_KEY_FINGERPRINT = PLAN_DRAFT_FINGERPRINT_KEY
_SNAPSHOT_KEY_FORBIDDEN = "forbidden_touched"
_SNAPSHOT_KEY_TIER = "tier"
_SNAPSHOT_KEY_FILES = "files"
_SNAPSHOT_KEY_LINES = "lines_estimate"


def _clean_review_snapshot(last_result: object) -> dict[str, object] | None:
    """Extract the clean-review predicate snapshot, or None if not fireable.

    Returns None (fail-closed) unless *last_result* is a dict at the
    ``review_pending_approval`` gate whose ``review``/``health``/``scope``
    sections are all present dicts. Whether the predicate *holds* is a
    separate check (:func:`_predicate_holds`) so detect and act share both the
    extraction and the decision.

    ``must_fix_initial`` and ``deferred`` are recorded for the audit trail
    only; neither is part of the predicate. A Large review reaches this gate
    only after its fix loop resolved every MUST_FIX, and a deferred finding is
    out-of-scope work recorded for follow-up, which is not a reason to stop
    shipping.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _REVIEW_PENDING_APPROVAL:
        return None
    review = last_result.get("review")
    health = last_result.get("health")
    scope = last_result.get("scope")
    if not (
        isinstance(review, dict)
        and isinstance(health, dict)
        and isinstance(scope, dict)
    ):
        return None
    return {
        "must_fix_initial": review.get("must_fix_initial"),
        "deferred": review.get("deferred", 0),
        "recommendation": health.get("recommendation"),
        "forbidden_touched": scope.get("forbidden_touched"),
        "agents_run": review.get("agents_run", 0),
    }


def _predicate_holds(snapshot: dict[str, object]) -> bool:
    """True iff the clean-review predicate is satisfied.

    Three fields decide it: the health recommendation is PROCEED, no
    forbidden area was touched, and at least one reviewer agent ran. A
    missing/None field (e.g. a malformed producer payload) fails the
    comparison and blocks the fire — the predicate is fail-closed.
    ``agents_run`` is guarded with an explicit ``isinstance`` check (rather
    than a bare ``> 0`` comparison) since *snapshot* is typed
    ``dict[str, object]`` — a malformed non-int producer value must fail
    closed, not raise or pass via truthy coercion. ``bool`` is excluded
    explicitly: it is a subclass of ``int`` in Python, so a malformed
    ``agents_run: true`` payload would otherwise satisfy both
    ``isinstance(agents_run, int)`` and ``agents_run > 0``.
    """
    agents_run = snapshot["agents_run"]
    return (
        snapshot["recommendation"] == _RECOMMENDATION_PROCEED
        and snapshot["forbidden_touched"] is False
        and isinstance(agents_run, int)
        and not isinstance(agents_run, bool)
        and agents_run > 0
    )


def _row_eligible(task: TicketTask, stage: Stage) -> bool:
    """True iff *task* is a row a recipe may release at *stage*'s gate.

    Two row-level exclusions shared by both recipes:

    - ``task.stage`` must be the stage the gate belongs to. An earlier-stage
      report (a sentinel that never reached ``task.stage``) is not that
      stage's gate, and approving it would advance the pointer past stage
      work that never ran.
    - ``task.scope_hint == "large"`` is the operator's own "gate this ticket"
      and is never released automatically.
    """
    return task.stage == stage and task.scope_hint != SCOPE_TIER_LARGE


def _plan_gate_snapshot(last_result: object) -> dict[str, object] | None:
    """Extract the plan-gate fields from *last_result*, or None if not fireable.

    Reads only the sentinel, never the tracker. Returns None (fail-closed)
    unless *last_result* is a dict at the ``plan_pending_approval`` gate with
    a dict ``scope`` section. Whether the predicate *holds* is
    :func:`_plan_predicate_holds`.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _PLAN_PENDING_APPROVAL:
        return None
    scope = last_result.get("scope")
    if not isinstance(scope, dict):
        return None
    return {
        _SNAPSHOT_KEY_TIER: scope.get("tier"),
        _SNAPSHOT_KEY_FILES: scope.get("files"),
        _SNAPSHOT_KEY_LINES: scope.get("lines_estimate"),
        _SNAPSHOT_KEY_FORBIDDEN: scope.get("forbidden_touched"),
        _SNAPSHOT_KEY_FINGERPRINT: last_result.get(PLAN_DRAFT_FINGERPRINT_KEY),
    }


def _plan_predicate_holds(snapshot: dict[str, object], task: TicketTask) -> bool:
    """True iff the plan gate may be released without a person.

    - No forbidden area is touched (``forbidden_touched`` is exactly False).
    - The draft carries a valid fingerprint. ``_approve_ticket_locked`` binds
      the row-path approval to it, and an approval bound to nothing is
      evidence the plan stage refuses, so releasing it would only re-park.
    - The row does not already hold an approval for this exact draft. If it
      does, the worker re-parked a plan that was already released, and
      releasing it again would loop, so a person must look.
    """
    fingerprint = snapshot[_SNAPSHOT_KEY_FINGERPRINT]
    if not isinstance(fingerprint, str) or not is_plan_draft_fingerprint(fingerprint):
        return False
    if (
        task.plan_approved_at is not None
        and task.plan_approved_fingerprint == fingerprint
    ):
        return False
    return snapshot[_SNAPSHOT_KEY_FORBIDDEN] is False
