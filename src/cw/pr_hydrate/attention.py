"""CI-rollup summary and the #929 attention-state decision table.

Part of the ``cw.pr_hydrate`` package (#2999). Ported from
``.claude/scripts/review_monitor_lib/attention.py``; see the package
docstring for why that script cannot be imported.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from cw.gh_check_vocabulary import (
    FAILED_CHECKRUN_CONCLUSIONS,
    FAILED_STATUS_CONTEXT_STATES,
    INFRA_ONLY_CHECKRUN_CONCLUSIONS,
    PENDING_CHECKRUN_STATUSES,
    CheckRunStatus,
    StatusContextState,
)

# Row-1 of the attention-state decision table (merge_blocked). BLOCKED is
# deliberately NOT in this set — it means "waiting on required reviews/checks,"
# not a code problem (see Rows 5a-5c).
# Why: this is a strict ALLOW-list, not a deny-list, so an UNKNOWN merge state
# never reads as merge_blocked. Ported review-monitor lesson (session:826a27f3,
# "mergeStateStatus can read UNKNOWN immediately after push/rebase"): GitHub
# computes mergeStateStatus asynchronously, so a check right after a push/rebase
# can transiently return UNKNOWN before DIRTY/CLEAN is determinable. Keeping the
# set to {DIRTY} means a not-yet-computed state can't misfire the
# escalate_merge_block recipe; the next poll re-hydrates the real value. See
# tests/test_pr_hydrate.py::TestAttentionState::
# test_row1_unknown_merge_state_not_merge_blocked.
_ROW1_MERGE_BLOCKING_STATES: frozenset[str] = frozenset({"DIRTY"})

# GitHub #1571 (#1535 drift-class instance 2) — the only non-None values
# _compute_attention_state's #929 decision table can return. Canonical home:
# this module owns the decision table. board.py derives its label-map key
# set from get_args(PrAttentionState) instead of hand-typing a duplicate copy.
PrAttentionState = Literal[
    "merge_blocked",
    "ci_failing",
    "changes_requested",
    "no_reviewer",
    "ready_to_approve",
]


def summarize_status_checks(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a ``statusCheckRollup`` list into a ``failing`` / ``pending`` summary.

    Ported verbatim from ``_summarize_status_checks`` in
    ``.claude/scripts/review_monitor_lib/attention.py`` (un-importable — lives
    outside src/).
    ``ok`` is the sole source of CI truth: in-progress/pending checks never block
    it, only genuine failures do.

    GitHub #1684: ``ok`` is False only when a *genuine*-failure conclusion
    (anything in ``FAILED_CHECKRUN_CONCLUSIONS`` other than
    ``INFRA_ONLY_CHECKRUN_CONCLUSIONS``) is present in ``failing`` — a run
    whose only failing conclusions are CANCELLED/STARTUP_FAILURE (a
    provider-side/infra event) reads as ``ok=True``. ``failing`` itself is
    unaffected: infra-only entries still populate it for evidence/display.
    """
    failing: list[dict[str, str]] = []
    pending_count = 0
    for c in rollup:
        typename = c.get("__typename", "")
        if typename == "CheckRun":
            status = (c.get("status") or "").upper()
            conclusion = (c.get("conclusion") or "").upper()
            if (
                status == CheckRunStatus.COMPLETED
                and conclusion in FAILED_CHECKRUN_CONCLUSIONS
            ):
                failing.append(
                    {
                        "workflow": c.get("workflowName") or "",
                        "name": c.get("name") or "",
                        "conclusion": conclusion,
                        "url": c.get("detailsUrl") or "",
                    }
                )
            elif status in PENDING_CHECKRUN_STATUSES:
                pending_count += 1
        else:
            state_str = (c.get("state") or "").upper()
            if state_str in FAILED_STATUS_CONTEXT_STATES:
                failing.append(
                    {
                        "workflow": "",
                        "name": c.get("context") or "",
                        "conclusion": state_str,
                        "url": c.get("targetUrl") or "",
                    }
                )
            elif state_str == StatusContextState.PENDING:
                pending_count += 1
    has_genuine_failure = any(
        f["conclusion"] not in INFRA_ONLY_CHECKRUN_CONCLUSIONS for f in failing
    )
    return {
        "failing": failing,
        "pending_count": pending_count,
        "ok": not has_genuine_failure,
    }


# RFC 0010 W2 comment-review gap (#1195): cw's own review skills emit this
# vocabulary (a leading "## Review:" heading, or a MUST_FIX/BLOCKING marker)
# when a blocking review is posted as a plain issue/PR comment rather than
# through GitHub's Request-changes review flow. Deliberately scoped to cw's
# own emitted vocabulary — does NOT catch free-form human prose reviews
# (ticket option (a); (b)/#1108's structured review-verdict artifact is the
# general-case follow-up).
_BLOCKING_COMMENT_MARKERS: frozenset[str] = frozenset({"MUST_FIX", "BLOCKING"})
_REVIEW_HEADING_PREFIX = "## Review:"


def _comment_body_is_blocking(body: str) -> bool:
    """True if *body* carries cw's own blocking-review vocabulary (#1195).

    Marker matching requires a boundary on both sides, where a boundary is
    "not a word character and not a hyphen" — plain ``\\b`` alone treats a
    hyphen as a boundary, which would false-positive on a negated,
    hyphen-joined prefix like ``"NON-BLOCKING"`` (a common code-review
    convention for explicitly marking a comment as *not* blocking).
    """
    if body.strip().startswith(_REVIEW_HEADING_PREFIX):
        return True
    return any(
        re.search(rf"(?<![\w-]){re.escape(marker)}(?![\w-])", body)
        for marker in _BLOCKING_COMMENT_MARKERS
    )


def _has_blocking_comment_review(
    comments: list[dict[str, Any]], *, self_login: str | None
) -> bool:
    """True if any non-self-authored comment carries a blocking-review marker.

    Pure classifier feeding the new row in ``_compute_attention_state``
    (#1195). *comments* is the raw ``gh pr view --json comments`` list (same
    ``body``/``author.login`` shape as ``fetch_approved_plan_comment``'s
    comment reads in ``cw.gh``); malformed entries are skipped, not raised
    on. *self_login* excludes the operator's own comments — without this, a
    past ``/address-review`` reply quoting "MUST_FIX" would re-trigger this
    classifier forever. *self_login* is caller-resolved (see
    ``hydrate_pr_states``), not fetched here.

    *self_login* is required to classify safely: when it is ``None`` (the
    caller could not resolve the operator's own identity that tick), this
    fails closed and returns ``False`` unconditionally rather than scanning
    without the exclusion — scanning anyway would reproduce the exact
    self-trigger loop the exclusion exists to prevent, on every tick identity
    resolution happens to fail.
    """
    if self_login is None:
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if login == self_login:
            continue
        body = comment.get("body")
        if isinstance(body, str) and _comment_body_is_blocking(body):
            return True
    return False


def _compute_attention_state(
    *,
    ci_ok: bool,
    pending_count: int,
    merge_state_status: str,
    review_decision: str,
    is_draft: bool,
    reviewer_count: int,
    has_blocking_comment_review: bool = False,
) -> PrAttentionState | None:
    """Derive the operator attention-state via the #929 decision table.

    Precedence chain + unconditional draft-gate ported from
    ``_compute_attention_state`` in
    ``.claude/scripts/review_monitor_lib/attention.py``. cw
    drops the reference's role/status/unaddressed_count inputs (no subsystem
    exists for them), but does carry a narrow comment-review input (#1195,
    row 2b) — see ``_has_blocking_comment_review``. First matching row wins:
      0. is_draft                                    -> None
      1. merge_state_status == DIRTY                 -> merge_blocked
      2. not ci_ok                                   -> ci_failing
      2b. has_blocking_comment_review                -> changes_requested
      3. review_decision == CHANGES_REQUESTED        -> changes_requested
      4. review_decision == REVIEW_REQUIRED and no reviewer -> no_reviewer
      5a. BLOCKED and pending checks                 -> None (waiting on CI)
      5b. BLOCKED and review required                -> ready_to_approve
      5c. BLOCKED otherwise                          -> None (unknown blocker)
      6. healthy default                             -> ready_to_approve

    Rows 5a-5c encode the #929 premise-round finding (2026-07-05): BLOCKED can
    co-occur with green CI (a required check that hasn't run yet, or missing
    approvals), so BLOCKED alone must not read as "ready to approve". Row 2b
    (#1195) is evaluated at equal precedence to Row 3 (immediately before it)
    so it overrides Rows 4/5/6 uniformly on both the BLOCKED and clean/
    ready_to_approve ladders — a comment-shaped blocking review must not be
    invisible just because it didn't arrive through GitHub's formal review
    flow.
    """
    if is_draft:  # Row 0 — drafts never enter an escalation path.
        # Why: unconditional draft-gate ported from review-monitor lesson
        # (session:fc766c55, "Draft PRs must not enter attention/escalation
        # paths"): a draft with green CI and a BLOCKED merge state would
        # otherwise fall through to ready_to_approve and trigger channel bumps /
        # auto-approve. is_draft short-circuits BEFORE every other row so no
        # draft ever produces an attention state. See tests/test_pr_hydrate.py::
        # TestAttentionState::{test_row0_draft_returns_none,
        # test_row0_gates_row4_draft_zero_reviewers}.
        return None
    if merge_state_status in _ROW1_MERGE_BLOCKING_STATES:  # Row 1
        return "merge_blocked"
    if not ci_ok:  # Row 2
        return "ci_failing"
    # Row 2b (#1195) is merged into Row 3's return (not a separate `if`/`return`)
    # to stay within the PLR0911 return-count budget — it is equal-precedence
    # to Row 3, not a distinct outcome.
    if has_blocking_comment_review or review_decision == "CHANGES_REQUESTED":  # Row 3
        # Why: fires on the PR's top-level reviewDecision, ported from review-
        # monitor lesson (session:1a93541b, "changes_requested fires on top-level
        # reviewDecision"): a "Request changes" review with NO inline comments
        # still moves reviewDecision to CHANGES_REQUESTED. cw has no inline-thread
        # subsystem, so this top-level signal is the sole changes_requested
        # trigger for the address_review recipe. See tests/test_pr_hydrate.py::
        # TestAttentionState::test_row3_changes_requested.
        return "changes_requested"
    if review_decision == "REVIEW_REQUIRED" and reviewer_count == 0:  # Row 4
        return "no_reviewer"
    # Rows 5a-5c (BLOCKED) delegate; anything else is Row 6's healthy default.
    return (
        _blocked_attention_state(
            pending_count=pending_count, review_decision=review_decision
        )
        if merge_state_status == "BLOCKED"
        else "ready_to_approve"
    )


def _blocked_attention_state(
    *, pending_count: int, review_decision: str
) -> PrAttentionState | None:
    """Rows 5a-5c: attention state for ``mergeStateStatus == BLOCKED``.

    5a — a required check is still running: waiting on CI, no attention state.
    5b — approvals outstanding: the one genuinely approvable BLOCKED shape.
    5c — blocked for an undetermined reason: don't overclaim approvability.
    """
    if pending_count > 0:  # 5a
        return None
    if review_decision == "REVIEW_REQUIRED":  # 5b
        return "ready_to_approve"
    return None  # 5c
