"""Attention-state computation from PR status, reviews and CI rollups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR


# States that indicate the author (user) needs to take action on their own PR.
USER_ATTENTION_STATES: frozenset[str] = frozenset(
    {"ready_to_approve", "ci_failing", "merge_blocked"}
)

# Minimum gap between local ping and Slack escalation for the same state.
ESCALATION_GRACE = timedelta(minutes=15)

_FAILED_CHECKRUN_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
)
_PENDING_CHECKRUN_STATUSES: frozenset[str] = frozenset(
    {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}
)


def _summarize_status_checks(rollup: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a ``statusCheckRollup`` list into a ``failing`` / ``pending`` summary.

    Rollup entries come in two shapes:
    - ``CheckRun`` (GitHub Actions): ``status`` + ``conclusion``.
    - ``StatusContext`` (legacy commit status / third-party): ``state``.

    Returns a dict with ``failing`` (list of {workflow,name,conclusion,url}),
    ``pending_count`` (int), and ``ok`` (bool).
    """
    failing: list[dict[str, str]] = []
    pending_count = 0
    for c in rollup:
        typename = c.get("__typename", "")
        if typename == "CheckRun":
            status = (c.get("status") or "").upper()
            conclusion = (c.get("conclusion") or "").upper()
            if status == "COMPLETED" and conclusion in _FAILED_CHECKRUN_CONCLUSIONS:
                failing.append(
                    {
                        "workflow": c.get("workflowName") or "",
                        "name": c.get("name") or "",
                        "conclusion": conclusion,
                        "url": c.get("detailsUrl") or "",
                    }
                )
            elif status in _PENDING_CHECKRUN_STATUSES:
                pending_count += 1
        else:
            state_str = (c.get("state") or "").upper()
            if state_str in ("FAILURE", "ERROR"):
                failing.append(
                    {
                        "workflow": "",
                        "name": c.get("context") or "",
                        "conclusion": state_str,
                        "url": c.get("targetUrl") or "",
                    }
                )
            elif state_str == "PENDING":
                pending_count += 1
    return {"failing": failing, "pending_count": pending_count, "ok": not failing}


def _resolve_change_request_source(
    attention_state: str | None,
    unaddressed_count: int,
    review_decision: str,
    has_actionable_comment_review: bool,
) -> str | None:
    """Resolve which signal earned the ``changes_requested`` state.

    Lets the skill route an auto-fix correctly — inline-thread reply vs.
    comment-review reply. Returns None when the PR is not in that state.
    """
    if attention_state != "changes_requested":
        return None
    if unaddressed_count > 0:
        return "inline"
    if review_decision == "CHANGES_REQUESTED":
        return "formal"
    if has_actionable_comment_review:
        return "comment"
    return None


# Attention states that warrant immediate Slack escalation (no grace period).
IMMEDIATE_ESCALATION_STATES: frozenset[str] = frozenset({"ci_failing", "merge_blocked"})


def _compute_attention_state(
    role: str,
    status: str,
    ci_ok: bool,
    merge_blocked: bool,
    merge_state_status: str = "UNKNOWN",
    unaddressed_count: int = 0,
    review_decision: str = "",
    has_actionable_comment_review: bool = False,
    is_draft: bool = False,
    reviewer_count: int = -1,
) -> str | None:
    """Return the highest-priority author-attention state, or None.

    Precedence (most urgent first):
      merge_blocked (DIRTY/BEHIND) → ci_failing → changes_requested (inline/formal)
      → changes_requested (comment-review fallback) → no_reviewer → ready_to_approve
      → None

    ``no_reviewer`` fires when a non-draft author PR still needs review
    (``reviewDecision == "REVIEW_REQUIRED"``) but has zero reviewers requested —
    an orphaned PR nobody was ever asked to look at. The skill clears it by
    requesting the default team. ``reviewer_count`` defaults to -1 ("not
    supplied") so the state only triggers when a caller explicitly passes 0.

    ``changes_requested`` fires on three signals, in order:
      1. An unresolved inline review thread.
      2. Top-level ``reviewDecision == "CHANGES_REQUESTED"`` (the reviewer hit
         "Request changes" without leaving inline comments).
      3. **Fallback:** ``has_actionable_comment_review`` — a non-bot
         ``COMMENTED`` review whose body the skill's classifier flagged as
         actually requesting changes. Last code-actionable signal before the
         PR is otherwise just "waiting for a formal review".

    BLOCKED merge state with green CI and no change-request is reclassified as
    ready_to_approve — it means "waiting for required reviews / branch protection,"
    not a code problem the author can fix. This routes it to the channel-bump path
    instead of auto-fix dispatch.

    Drafts (``is_draft=True``) always return None — they are author-controlled
    WIP and must not enter any escalation path (channel bump, DM, auto-fix).
    The draft-promotion logic in Step 4e handles their lifecycle separately.
    """
    if role != "author" or is_draft:
        return None
    # Precedence chain, most urgent first. BLOCKED + ci_ok + no change-request
    # reclassifies as ready_to_approve — waiting for review, not a code problem.
    precedence: list[tuple[bool, str]] = [
        (merge_blocked and merge_state_status in ("DIRTY", "BEHIND"), "merge_blocked"),
        (not ci_ok, "ci_failing"),
        (
            unaddressed_count > 0 or review_decision == "CHANGES_REQUESTED",
            "changes_requested",
        ),
        (has_actionable_comment_review, "changes_requested"),
        (review_decision == "REVIEW_REQUIRED" and reviewer_count == 0, "no_reviewer"),
        (merge_state_status == "BLOCKED", "ready_to_approve"),
        (status == "ready_to_approve", "ready_to_approve"),
    ]
    for matched, attention_state in precedence:
        if matched:
            return attention_state
    return None


def _compute_needs_escalation(pr: MonitoredPR, attention_state: str | None) -> bool:
    """Return True when /review-monitor should fire a Slack-bot escalation.

    Rules:
      - No attention state → never.
      - Immediate-escalation states (ci_failing, merge_blocked) → fire once per
        state transition.
      - ready_to_approve → fire once the 15-min grace elapses after local ping,
        and only if we haven't already escalated for this state.
    """
    if attention_state is None:
        return False
    # no_reviewer self-heals — the skill requests the default team the same
    # cycle it surfaces, so the state clears before any DM is warranted.
    if attention_state == "no_reviewer":
        return False
    already_escalated_this_state = (
        pr.last_escalated_at is not None
        and pr.last_notified_at is not None
        and pr.last_escalated_at >= pr.last_notified_at
        and pr.last_notified_state == attention_state
    )
    if already_escalated_this_state:
        return False
    if attention_state in IMMEDIATE_ESCALATION_STATES:
        return True
    # Grace-based: require a prior local ping for THIS state, and enough elapsed time.
    if pr.last_notified_state != attention_state or pr.last_notified_at is None:
        return False
    last_notified = datetime.fromisoformat(pr.last_notified_at)
    return datetime.now(UTC) - last_notified >= ESCALATION_GRACE
