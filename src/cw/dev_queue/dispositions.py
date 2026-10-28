"""Dev-queue disposition vocabulary and sentinel-derivation helpers.

Split out of ``lifecycle`` (#2719) to keep that module under the size ceiling.
Owns the ``TicketTask.disposition`` string constants (plus the two
``_PRE_DISPATCH_*`` ``blocked_reason`` literals stamped beside them),
``HOLD_DISPOSITIONS``, and the pure helpers that derive a disposition or a PR
URL from a session's sentinel (``_derive_disposition``,
``_hold_aware_disposition``, ``_result_blocker_reason``, ``_extract_pr_url``,
``_extract_pr_url_or_info``).

A leaf: it imports only ``cw.auto_dev_result``, so ``lifecycle``, ``gates``,
``next_commands`` and ``dispatch.claim.screening`` import it at module level
without a cycle. Nothing here does I/O or is a monkeypatch target.
"""

from __future__ import annotations

from typing import Final

from cw.auto_dev_result import (
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    AutoDevResult,
    BlockedResult,
)

# Disposition stamped when a ticket is parked AWAITING_OPERATOR_SIGNOFF by
# either dispatch's staged-decision routing (RFC 0007 Phase 3, dispatch.py) or
# approve_ticket's own REVIEW-stage re-check below. Imported by dispatch.py via
# a function-level import to break the dev_queue<->dispatch circularity
# (mirrors reconcile/_shared/_routing.py's precedent for the same import shape).
SIGNOFF_GATE_DISPOSITION = "signoff_gate"

# Disposition stamped when a park is a *hold* -- "we could not reach the
# operator or a dependency" (e.g. blocker.reason == "dependency_unmerged",
# #2260, or "confirm_backend_unavailable", #2787), not "this leg is broken"
# (RFC 0011 A1, #1254). Textually distinct
# from dispatch.routing._AWAITING_OPERATOR_REASON
# ("awaiting_operator_availability"): that one is a SESSION_NEEDS_ATTENTION
# paused_status string, this one classifies TicketTask.disposition. Different
# namespaces -- do not confuse them.
AWAITING_OPERATOR_DISPOSITION = "awaiting_operator"

# Disposition stamped when a park is a *proactive* hold -- "an operator asked
# this ticket to stop before an unattended finalize", not "we could not reach
# anyone" (RFC 0011 A3, #1160). Textually distinct from
# dispatch.review_gates._FINALIZE_HOLD_REASON ("finalize_hold", moved from
# dispatch.routing by #1823): that one is a SESSION_NEEDS_ATTENTION
# paused_status string, this one classifies TicketTask.disposition. Different
# namespaces -- do not confuse them. Released by an explicit
# ``cw dev-queue approve``.
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
# It is stamped directly by
# dispatch.routing.stage_failure._park_must_fix_mechanically_rejected, Rule 5's
# sole reason-keyed override -- deliberately, so it can
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

# Disposition stamped when dispatch's REVIEW-stage routing refuses to advance a
# ticket whose branch is behind origin/<default_branch> AND whose intervening
# main commits touch a file the branch itself touches (#1823). The narrow,
# file-overlap-scoped rule -- a branch that has merely fallen behind, with
# disjoint churn, is not parked.
#
# Shares its literal string value with dispatch.review_gates.
# _BRANCH_STALENESS_REASON (a SESSION_NEEDS_ATTENTION paused_status) by the
# same deliberate choice REVIEW_HEALTH_GATE_DISPOSITION / _REVIEW_HEALTH_GATE_
# REASON already make -- still two constants in two namespaces, do not collapse
# them.
#
# Same set-membership treatment as the two dispositions above, for the same
# reason. Deliberately NOT a HOLD_DISPOSITIONS member: a stale branch clears by
# rebasing and re-running, not by an operator saying "proceed anyway", and
# membership would also silently make it eligible for concierge's false-park
# auto-requeue recipe (same _REAP_ELIGIBLE_DISPOSITIONS_BASE lineage), which
# would spin a genuinely stale branch back through the pipeline and defeat the
# gate outright.
BRANCH_STALENESS_GATE_DISPOSITION = "branch_behind_main"

# Disposition stamped when dispatch's REVIEW-stage routing refuses to advance a
# ticket whose branch measures zero commits ahead of origin/<default_branch>
# (#1870) -- there is no diff to review and nothing to ship, so the Stage 4
# approval prompt would be asking an operator to authorize nothing.
#
# Shares its literal string value with dispatch.review_gates.
# _EMPTY_DIFF_GATE_REASON (a SESSION_NEEDS_ATTENTION paused_status) on the same
# precedent as BRANCH_STALENESS_GATE_DISPOSITION above -- still two constants in
# two namespaces, do not collapse them. Both are deliberately distinct from the
# producer-reported "empty_diff_blocked" *status* literal: collapsing the gate's
# paused_status onto the status would put a gate-class park (which hardcodes
# breadcrumbs="") into BREADCRUMB_ELIGIBLE_PAUSED_STATUSES, contradicting the
# #1729 convention that set is pinned against.
#
# Same set-membership treatment as the three dispositions above, for the same
# reason. Deliberately NOT a HOLD_DISPOSITIONS member: an empty branch clears by
# pushing real commits (or closing the ticket), not by an operator saying
# "proceed anyway", and membership would also silently make it eligible for
# concierge's false-park auto-requeue recipe (same _REAP_ELIGIBLE_DISPOSITIONS_
# BASE lineage), which would spin an empty branch straight back through the
# pipeline and defeat the gate outright.
EMPTY_DIFF_GATE_DISPOSITION = "empty_diff_gate"

# Disposition stamped when dispatch's REVIEW-stage routing refuses to advance a
# ticket whose sentinel does not carry a review.reviewed_sha matching the
# worktree's live HEAD (#2123) -- the reviewers vouched for a tree that is not
# the tree that would ship. Missing, non-string, and unmeasurable all land
# here too: this gate fails CLOSED, unlike its two git-measured siblings above,
# because "is this review current?" has no safe answer without evidence.
#
# The hole it closes: a review-stage park released via `requeue` re-dispatches
# and can land back on `review_pending_approval` with no reviewer having run
# against the current HEAD. Every field the auto-approve recipe checks reads
# clean in that state, because those numbers describe the *earlier* tree.
#
# Shares its literal string value with dispatch.review_gates.
# _REVIEW_STALENESS_REASON (a SESSION_NEEDS_ATTENTION paused_status) on the same
# precedent as EMPTY_DIFF_GATE_DISPOSITION above -- still two constants in two
# namespaces, do not collapse them.
#
# Same set-membership treatment as the four dispositions above: deliberately
# NOT a HOLD_DISPOSITIONS member, since stale review artifacts clear by
# re-running review rather than by an operator saying "proceed anyway", and
# membership would silently make the row eligible for concierge's false-park
# auto-requeue recipe (same _REAP_ELIGIBLE_DISPOSITIONS_BASE lineage) and
# defeat the gate. It DOES join DRAIN_DISPOSITIONS, unlike
# BRANCH_STALENESS_GATE_DISPOSITION -- see the note in drain.py for why the two
# staleness gates diverge there.
REVIEW_STALENESS_GATE_DISPOSITION = "review_artifacts_stale"

# Disposition stamped when dispatch's IMPL-stage routing refuses to advance a
# FINALIZE-regress round trip whose merge was left unconcluded (#2421): the
# worktree is still mid-merge, or the merge-caused regress landed no commit.
# Shares its literal with dispatch.impl_gates._UNCONCLUDED_MERGE_REASON (two
# namespaces, do not collapse). Same set-membership treatment as the gate
# dispositions above, for the same reason: NOT a HOLD_DISPOSITIONS member -- a
# half-finished merge clears by committing it, not by "proceed anyway".
UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION = (
    "unconcluded_finalize_regress_merge"
)

# Disposition stamped when a session reported the ``stale_dispatch`` sentinel
# itself -- an agent ran, discovered this ticket already has an open, unmerged
# PR from an earlier dispatch, and refused rather than re-implementing on top
# of work already in review (#1862).
#
# Unlike the four gate dispositions above, this one is NOT stamped directly:
# it is the verbatim ``Status`` value ``_derive_disposition`` returns, because
# ``"stale_dispatch"`` is a ``STAGE_FAILURE_STATUSES`` member. The constant
# exists so call sites and tests name the literal once, and so the lockstep
# test in test_dev_queue.py can pin it against ``get_args(Status)``.
STALE_DISPATCH_DISPOSITION: Final = "stale_dispatch"

# Disposition stamped when dispatch's *pre-dispatch* open-PR gate refuses to
# claim a PLAN/IMPL-stage PENDING task whose branch already has an open PR
# (#1862). No agent ever ran for this park -- the gate fires in
# ``_claim_next_pending`` before any session is spawned.
#
# Deliberately a SECOND literal, distinct from STALE_DISPATCH_DISPOSITION
# above, on the #1729 precedent EMPTY_DIFF_GATE_DISPOSITION already sets (see
# its comment, and dispatch.review_gates' module comment). It is never added
# to ``Status``, ``STAGE_FAILURE_STATUSES``, ``SALVAGE_TERMINAL_STATUSES``, or
# any other ``Status``-derived set -- so it never becomes
# BREADCRUMB_ELIGIBLE_PAUSED_STATUSES-eligible, matching that this park
# hardcodes ``breadcrumbs=""`` and structurally cannot carry one. One literal
# for both would put the two in conflict.
#
# Same set-membership treatment as the gate dispositions above: deliberately
# NOT a HOLD_DISPOSITIONS member. An already-open PR clears by landing or
# closing that PR, not by an operator saying "proceed anyway", and membership
# would make it eligible for concierge's false-park auto-requeue recipe --
# which would re-dispatch the exact ticket this gate exists to hold back.
STALE_DISPATCH_GATE_DISPOSITION: Final = "stale_dispatch_gate"

# ``TicketTask.blocked_reason`` stamped alongside
# STALE_DISPATCH_GATE_DISPOSITION (#1862). Deliberately distinct from
# ``cw.auto_dev_result.STALE_DISPATCH_BLOCKER_REASON`` ("pr_already_open"),
# which an *agent* emits on its sentinel: the ``_pre_dispatch`` suffix is how
# an operator reading ``cw dev-queue status`` tells "the loop refused to spawn
# this" apart from "a session ran and reported the conflict". Different
# namespaces -- do not collapse them.
_PRE_DISPATCH_STALE_PR_REASON: Final = "pr_already_open_pre_dispatch"

# Disposition stamped when dispatch's pre-dispatch tracker-MCP gate refuses to
# claim a PLAN/IMPL-stage PENDING task whose branch's settings file verifiably
# lacks the client's configured tracker MCP plugin (#2442). No agent ever ran
# for this park -- like STALE_DISPATCH_GATE_DISPOSITION it fires in
# ``_claim_next_pending`` before any session is spawned, carries
# ``breadcrumbs=""``, and is never added to ``Status`` or any
# ``Status``-derived set.
#
# Deliberately NOT a HOLD_DISPOSITIONS member, on the stale-PR gate's
# precedent: it clears by fixing the branch's settings file (or disabling the
# client's gate) and re-dispatching, not by an operator saying "proceed
# anyway" -- and membership would make it eligible for concierge's false-park
# auto-requeue recipe, re-dispatching into the same missing plugin.
TRACKER_MCP_GATE_DISPOSITION: Final = "tracker_mcp_gate"

# ``TicketTask.blocked_reason`` stamped alongside TRACKER_MCP_GATE_DISPOSITION
# (#2442). The ``_pre_dispatch`` suffix follows _PRE_DISPATCH_STALE_PR_REASON's
# convention: it tells "the loop refused to spawn this" apart from anything an
# agent might report on its own sentinel.
_PRE_DISPATCH_TRACKER_MCP_REASON: Final = "tracker_mcp_unavailable_pre_dispatch"

# Textually identical to cw.reconcile._shared._NEEDS_SALVAGE_REASON
# ("needs_salvage") but a SEPARATE constant, not an import of it: _shared
# imports FROM cw.dev_queue (dev_queue -> reconcile is the only cycle-safe
# direction), so this module cannot import the reconcile-side copy without
# a cycle. Unlike AWAITING_OPERATOR_DISPOSITION/FINALIZE_GATE_HELD_DISPOSITION
# above (deliberately DISTINCT values from their dispatch.routing namesakes --
# safe to diverge), this constant's value must stay identical to
# _NEEDS_SALVAGE_REASON for the seam guard in lifecycle.transition_task_status
# to keep matching salvage.py's call; test_dev_queue.py asserts the two literals
# in lockstep so a future edit to either fails loudly instead of silently
# breaking the stamp.
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


def _extract_pr_url_or_info(last_result: dict[str, object] | None) -> str | None:
    """Like :func:`_extract_pr_url`, with a ``pr_info`` fallback (GitHub #1713).

    ``pr`` is schema-forbidden from being non-null when ``status=="blocked"``
    (``_check_status_pairings``), so an ``automerge_not_armed`` park -- a
    ``blocked`` status carrying a real, already-created PR -- can never
    populate ``pr.url``. The producer instead emits the PR's identity in a
    separate, unmodeled ``pr_info`` object (``.claude/commands/
    auto-dev-finalize.md``'s ``automerge_not_armed`` sentinel template:
    ``pr_info: {number, url, auto_merge, base}``). This helper tries the
    modeled ``pr`` field first (shipped/merge_pending's normal shape), then
    falls back to ``pr_info.url`` so the ``automerge_not_armed`` park can also
    stamp ``TicketTask.pr_url`` and become hydratable by
    ``cw.pr_hydrate._is_candidate`` (previously permanently skipped -- see
    that predicate's ``if not task.pr_url: return False`` gate).
    """
    url = _extract_pr_url(last_result)
    if url is not None:
        return url
    if last_result is None:
        return None
    pr_info = last_result.get("pr_info")
    if isinstance(pr_info, dict):
        info_url = pr_info.get("url")
        return str(info_url) if info_url is not None else None
    return None
