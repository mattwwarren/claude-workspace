"""Approval-gate and drain-set predicates shared across ``cw.dev_queue`` (#2692).

A leaf module: it imports only ``dispositions`` constants and
``cw.auto_dev_result``, never ``approval``, ``drain``, ``requeue`` or
``cw.dispatch``. That is what lets ``next_commands`` (and, through it, every
refusal site in ``approval``, ``requeue``, ``crud`` and ``must_fix_override``)
import these at module level without the ``approval -> next_commands ->
approval`` and ``requeue -> next_commands -> drain -> requeue`` cycles.

It is also the single definition of the ``"approval_gate"`` string
(``APPROVAL_GATE_REASON``). ``cw.dispatch.routing`` binds its historical
``_APPROVAL_GATE_REASON`` name to it, so the literal lives in one place and
``_not_at_approval_gate`` needs no deferred ``cw.dispatch`` import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES
from cw.dev_queue.dispositions import (
    BRANCH_STALENESS_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    HOLD_DISPOSITIONS,
    REVIEW_HEALTH_GATE_DISPOSITION,
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
)

if TYPE_CHECKING:
    from cw.models import Session, TicketTask

# Both the TicketTask.disposition `_park_scope_hint_gate` stamps on a
# scope_hint escalation park (#1640) and the SESSION_NEEDS_ATTENTION
# `paused_status` dispatch emits for it.
APPROVAL_GATE_REASON = "approval_gate"

# Status vocabulary used by the dev-queue refusal table. Keep these public so
# guidance keys use shared identifiers rather than copying status strings in a
# consumer.
AMBIGUITIES_PENDING_RESOLUTION_STATUS: Final = "ambiguities_pending_resolution"
PREMISES_PENDING_VERIFICATION_STATUS: Final = "premises_pending_verification"
MERGE_GATE_BLOCKED_STATUS: Final = "merge_gate_blocked"

# RFC 0011 A4 R11: drain's selection is the Rule-5 availability-park subset
# of HOLD_DISPOSITIONS, EXCLUDING the A3 force-hold disposition (#1160's
# FINALIZE_GATE_HELD_DISPOSITION) -- a force hold is never batch-released.
# Derived by subtraction now that #1160 has landed (the original hand-pin
# existed only because the constant wasn't importable yet); a future hold
# disposition added to HOLD_DISPOSITIONS therefore flows into drain's
# selection automatically -- exclude it here explicitly if it must not be
# batch-releasable, mirroring the A3 exclusion.
#
# #1702 adds REVIEW_HEALTH_GATE_DISPOSITION by explicit union rather than
# through HOLD_DISPOSITIONS (it is deliberately not a member of that set, see
# lifecycle.py). The two exclusion/inclusion calls have opposite answers for a
# reason: an A3 force hold is a deliberate operator stop, so batch-releasing it
# would override the operator; a review-health park is "the review that ran did
# not vouch for its own coverage", which clears by re-running review -- exactly
# what drain does. Including it here does NOT make it concierge-false-park-
# eligible; those sets are independent by design.
# #1714 joins on the same terms as #1702's union above and for the same reason:
# a mechanically-rejected MUST_FIX park says "review dropped a finding before
# adjudicating it", which clears by re-running review -- exactly what drain
# does. Also not an operator stop, so batch-releasing it overrides nobody.
# #2123 joins on those same terms, and the contrast with
# BRANCH_STALENESS_GATE_DISPOSITION -- still deliberately absent from this set
# -- is the load-bearing part. A stale *branch* needs an operator rebase before
# a re-run means anything, so batch-releasing it would spin the same stale tree
# back through the pipeline. Stale review *artifacts* need only a fresh review
# against the current HEAD, which is precisely what a drain-triggered requeue
# produces; and if that re-run is itself still stale, the gate re-fires rather
# than advancing.
DRAIN_DISPOSITIONS: frozenset[str] = (
    HOLD_DISPOSITIONS - frozenset({FINALIZE_GATE_HELD_DISPOSITION})
) | frozenset(
    {
        REVIEW_HEALTH_GATE_DISPOSITION,
        REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
        REVIEW_STALENESS_GATE_DISPOSITION,
    }
)


def _not_at_approval_gate(session: Session, task: TicketTask) -> bool:
    """True iff neither release condition for the approval gate is met.

    Extracted from ``_approve_ticket_locked`` to keep that function under the
    PLR0915 statement ceiling (#1640), and moved here from ``approval`` so
    ``next_commands`` can share it without an import cycle (#2692). Two
    independent conditions can satisfy the gate: the session's
    ``last_result`` status is one of ``SCOPE_GATED_APPROVAL_STATUSES`` (the
    ``plan_pending_approval`` / ``review_pending_approval`` release path), or
    the task's ``disposition`` records a park this entry point is authorised
    to release -- ``APPROVAL_GATE_REASON`` (the ``scope_hint`` escalation
    gate, GitHub #1640) or ``FINALIZE_GATE_HELD_DISPOSITION`` (the RFC 0011 A3
    force hold, GitHub #2410 -- a plain ``stage_complete`` completion never
    satisfies the status condition, so without this the operator-initiated
    bypass inside ``_approve_ticket_locked`` is never reached). ``approve``
    proceeds if either condition holds -- unless the #1823 branch-staleness
    override below fires first, which vetoes both.
    """
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
    not_at_disposition_gate = task.disposition not in (
        APPROVAL_GATE_REASON,
        FINALIZE_GATE_HELD_DISPOSITION,
    )
    return not_at_status_gate and not_at_disposition_gate
