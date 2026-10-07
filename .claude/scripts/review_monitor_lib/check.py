"""The ``check`` subcommand: one monitoring cycle for a PR."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from review_monitor_lib.attention import (
    _compute_attention_state,
    _compute_needs_escalation,
    _resolve_change_request_source,
    _summarize_status_checks,
)
from review_monitor_lib.comment_reviews import (
    _collect_pending_comment_reviews,
    _refresh_comment_reviews,
)
from review_monitor_lib.delta import _detect_touched_threads
from review_monitor_lib.escalation import (
    _auto_fix_attempts_today,
    _business_minutes_in_state,
    _compute_auto_fix_ok,
    _dm_escalation_reason,
    _ensure_state_entered_at,
    _needs_channel_bump,
    _reset_auto_fix_counter_if_stale,
)
from review_monitor_lib.shell import _get_our_username, _run_gh
from review_monitor_lib.state import load_state, save_state
from review_monitor_lib.threads import (
    _apply_status_transitions,
    _collect_deferred_threads_for_followup,
    _refresh_threads,
)

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR, MonitorState


_BLOCKING_MERGE_STATES: frozenset[str] = frozenset({"DIRTY", "BEHIND", "BLOCKED"})


def _complete_terminal_pr(
    state: MonitorState,
    key: str,
    pr: MonitoredPR,
    pr_number: int,
    pr_state: str,
    repo: str,
) -> dict[str, Any]:
    """Complete a MERGED/CLOSED PR and return its terminal check result."""
    deferred_threads_out: list[dict[str, Any]] = []
    # Only surface deferred follow-ups for actually-merged PRs. A CLOSED
    # (un-merged) PR is abandoned work — its deferrals went with it.
    if pr_state == "MERGED":
        deferred_threads_out = _collect_deferred_threads_for_followup(pr, pr_number)
    state.complete_pr(key, pr_state)
    save_state(state, repo)
    return {
        "pr_number": pr_number,
        "pr_state": pr_state,
        "completed": True,
        "reason": pr_state,
        "deferred_threads": deferred_threads_out,
    }


def _derive_check_signals(
    pr: MonitoredPR,
    pr_view: dict[str, Any],
    *,
    is_draft: bool,
    has_prior_human_review: bool,
) -> dict[str, Any]:
    """Compute the attention/escalation signals for a live (non-terminal) PR.

    Mutates ``pr`` (awaiting_rereview, state-entered timestamp, auto-fix
    counter) — the caller persists afterwards. Returns a dict keyed by the
    ``cmd_check`` result keys this step contributes.
    """
    rollup: list[dict[str, Any]] = pr_view.get("statusCheckRollup") or []
    ci_summary = _summarize_status_checks(rollup)
    merge_state_status: str = pr_view.get("mergeStateStatus") or "UNKNOWN"
    merge_blocked = merge_state_status in _BLOCKING_MERGE_STATES
    review_decision: str = pr_view.get("reviewDecision") or ""

    unaddressed_count = len(pr.unaddressed_threads())
    has_actionable_comment_review = any(
        ref.classification == "requests_changes" for ref in pr.comment_reviews.values()
    )
    reviewer_count = len(pr_view.get("reviewRequests") or [])
    attention_state = _compute_attention_state(
        role=pr.role,
        status=pr.status,
        ci_ok=ci_summary["ok"],
        merge_blocked=merge_blocked,
        merge_state_status=merge_state_status,
        unaddressed_count=unaddressed_count,
        review_decision=review_decision,
        has_actionable_comment_review=has_actionable_comment_review,
        is_draft=is_draft,
        reviewer_count=reviewer_count,
    )

    # "Awaiting re-review" only applies once the PR is otherwise mergeable and
    # just waiting on review — not while CI / threads still need author work.
    pr.awaiting_rereview = (
        has_prior_human_review and attention_state == "ready_to_approve"
    )
    _ensure_state_entered_at(pr, attention_state)
    _reset_auto_fix_counter_if_stale(pr)

    base_ref_name: str = pr_view.get("baseRefName") or ""
    auto_fix_ok, auto_fix_blocked_reason = _compute_auto_fix_ok(
        pr, is_draft, base_ref_name
    )
    return {
        "failing_checks": ci_summary["failing"],
        "pending_checks_count": ci_summary["pending_count"],
        "ci_ok": ci_summary["ok"],
        "merge_state_status": merge_state_status,
        "merge_blocked": merge_blocked,
        "attention_state": attention_state,
        "awaiting_rereview": pr.awaiting_rereview,
        "reviewer_count": reviewer_count,
        "needs_local_ping": attention_state is not None
        and pr.last_notified_state != attention_state,
        "needs_escalation": _compute_needs_escalation(pr, attention_state),
        "auto_fix_ok": auto_fix_ok,
        "auto_fix_blocked_reason": auto_fix_blocked_reason,
        "business_minutes_in_state": _business_minutes_in_state(pr)
        if attention_state
        else 0,
        "needs_channel_bump": _needs_channel_bump(pr, attention_state),
        "dm_escalation_reason": _dm_escalation_reason(pr, attention_state),
        "head_ref_name": pr_view.get("headRefName") or "",
        "base_ref_name": base_ref_name,
        "change_request_source": _resolve_change_request_source(
            attention_state,
            unaddressed_count,
            review_decision,
            has_actionable_comment_review,
        ),
        "pending_comment_reviews": _collect_pending_comment_reviews(
            pr,
            unaddressed_count=unaddressed_count,
            review_decision=review_decision,
            merge_blocked=merge_blocked,
            merge_state_status=merge_state_status,
            ci_ok=ci_summary["ok"],
        ),
    }


def cmd_check(pr_number: int, repo: str) -> dict[str, Any]:
    """Run one monitoring cycle for a single PR.

    Calls GitHub APIs and, when new commits are detected, ``git diff`` to
    update thread and code-change status.

    Returns a structured dict with the check results.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}

    pr = state.monitored[key]

    # 1. Fetch current PR state from GitHub
    pr_view_raw = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "headRefOid,headRefName,baseRefName,isDraft,state,mergedAt,closedAt,statusCheckRollup,mergeStateStatus,reviewDecision,reviewRequests",
        ],
        repo=pr.repo,
    )
    try:
        pr_view: dict[str, Any] = json.loads(pr_view_raw)
    except (json.JSONDecodeError, ValueError):
        pr_view = {}

    pr_state: str = pr_view.get("state", "UNKNOWN")
    new_sha: str = pr_view.get("headRefOid", pr.last_seen_sha)

    # 2. Handle terminal states
    if pr_state in ("MERGED", "CLOSED"):
        return _complete_terminal_pr(state, key, pr, pr_number, pr_state, repo)

    # 3. Detect new commits
    old_sha = pr.last_seen_sha
    changed = new_sha != old_sha

    # 4-6. Fetch thread / review data and update tracked threads
    our_username = _get_our_username()
    thread_updates = _refresh_threads(pr, pr_number, our_username)

    # 7. Touched-thread detection (delta since the delta baseline + local repo).
    has_delta_diff, delta_diff, touched_threads = _detect_touched_threads(
        pr, delta_base_sha=pr.delta_base_sha, new_sha=new_sha
    )

    is_draft: bool = bool(pr_view.get("isDraft"))

    # 8. Status transitions
    _apply_status_transitions(pr, changed, is_draft=is_draft)

    # 8b. Refresh tracked comment reviews (fallback change-request signal).
    has_prior_human_review = False
    if pr.role == "author":
        has_prior_human_review = _refresh_comment_reviews(
            pr, repo, pr_number, sha_changed=changed, our_username=our_username
        )

    # 9. Persist updated state.
    #
    # ``last_seen_sha`` tracks observed HEAD — advance every cycle.
    #
    # ``delta_base_sha`` is the delta-review baseline. A reviewer delta is a
    # *lossy* read: Step 3's delta review is a best-effort LLM step, so the
    # baseline only advances once the skill positively acks it (``ack-delta``).
    # Advancing it here would drop an unprocessed delta permanently. With no
    # delta to consume (author PRs, or an empty reviewer diff) it advances now.
    pr.last_seen_sha = new_sha
    if not (pr.role == "reviewer" and has_delta_diff):
        pr.delta_base_sha = new_sha
    pr.last_checked_at = datetime.now(UTC).isoformat()
    save_state(state, repo)

    # 10. Build result dict
    signals = _derive_check_signals(
        pr, pr_view, is_draft=is_draft, has_prior_human_review=has_prior_human_review
    )
    save_state(state, repo)

    result: dict[str, Any] = {
        "pr_number": pr_number,
        "pr_state": pr_state,
        "changed": changed,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "role": pr.role,
        "status": pr.status,
        "thread_updates": thread_updates,
        "all_addressed": pr.all_threads_addressed(),
        "unaddressed": pr.unaddressed_threads(),
        "touched_threads": touched_threads,
        "has_delta_diff": has_delta_diff,
        "slack_channel": pr.slack_channel,
        "slack_ts": pr.slack_ts,
        "slack_last_seen_ts": pr.slack_last_seen_ts,
        "auto_fix_attempts_today": _auto_fix_attempts_today(pr),
        "is_draft": is_draft,
        **signals,
    }
    if pr.role == "reviewer" and has_delta_diff:
        result["delta_diff"] = delta_diff
    return result
