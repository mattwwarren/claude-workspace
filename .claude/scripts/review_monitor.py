#!/usr/bin/env python3
"""
Review monitor: track PR review threads and nudge authors/reviewers.

Subcommands (to be added in subsequent tasks):
  register  — Start monitoring a PR
  drop      — Stop monitoring a PR
  complete  — Mark a PR as done
  status    — Show current monitor state
  check     — Run one monitoring cycle (resolve threads, detect deferrals, nudge)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from review_monitor_lib.models import MonitoredPR

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_monitor_lib.attention import (
    _compute_attention_state,
    _compute_needs_escalation,
    _resolve_change_request_source,
    _summarize_status_checks,
)
from review_monitor_lib.comment_reviews import (
    _collect_pending_comment_reviews,
    _refresh_comment_reviews,
    cmd_mark_comment_review,
)
from review_monitor_lib.delta import _detect_touched_threads
from review_monitor_lib.escalation import (
    AUTO_FIX_DAILY_CAP,
    _auto_fix_attempts_today,
    _business_minutes_in_state,
    _compute_auto_fix_ok,
    _dm_escalation_reason,
    _ensure_state_entered_at,
    _needs_channel_bump,
    _reset_auto_fix_counter_if_stale,
)
from review_monitor_lib.lifecycle import (
    cmd_ack_delta,
    cmd_complete,
    cmd_confirm_thread,
    cmd_drop,
    cmd_register,
    cmd_set_status,
    cmd_slack_thread_cursor,
    cmd_update_slack_cursor,
)
from review_monitor_lib.models import MonitorState
from review_monitor_lib.shell import _get_our_username, _run_gh
from review_monitor_lib.state import cmd_list_repos, load_state, save_state
from review_monitor_lib.threads import (
    _apply_status_transitions,
    _collect_deferred_threads_for_followup,
    _extract_login,
    _refresh_threads,
)
from utils.runtime_paths import desktop_queue_dir

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# Minimum time between nudge messages for the same PR
NUDGE_COOLDOWN = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHub / git helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _nudge_activity_check(pr_number: int, repo: str) -> dict[str, Any] | None:
    """Return a blocking ``{"allowed": False, ...}`` if PR activity suppresses a nudge.

    No nudge if the PR was updated within the last 24h. Fails **closed**: an
    unreachable or unparseable gh response blocks the nudge rather than risking
    a premature one. Returns ``None`` when a nudge is not blocked on this basis.
    """
    updated_raw = _run_gh(
        ["pr", "view", str(pr_number), "--json", "updatedAt"], repo=repo
    )
    if not updated_raw:
        return {
            "allowed": False,
            "reason": "could not fetch PR activity — skipping nudge to be safe",
        }
    try:
        updated_at_str: str = json.loads(updated_raw)["updatedAt"]
        updated_at = datetime.fromisoformat(updated_at_str)
    except (json.JSONDecodeError, ValueError, KeyError):
        return {
            "allowed": False,
            "reason": "could not parse PR activity — skipping nudge to be safe",
        }
    if datetime.now(UTC) - updated_at < NUDGE_COOLDOWN:
        return {"allowed": False, "reason": "PR has recent activity"}
    return None


def cmd_nudge_ok(pr_number: int, repo: str) -> dict[str, Any]:
    """Return whether a nudge is allowed for the given PR.

    Returns ``{"allowed": True/False, "reason": "..."}``.
    Allowed when ALL hold:

    - *last_nudge_at* is ``None`` or 24+ hours ago (cooldown);
    - the PR has been monitored for 24+ hours (grace period — the author needs
      a fair chance to respond before a first nudge);
    - the PR has had no activity (commits or comments) in the last 24 hours.

    The activity check fails **closed**: if PR activity cannot be determined,
    the nudge is skipped rather than risk a premature one.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"allowed": False, "reason": f"PR {key} not found in monitored"}
    pr = state.monitored[key]

    # Cooldown check
    if pr.last_nudge_at is not None:
        last = datetime.fromisoformat(pr.last_nudge_at)
        elapsed = datetime.now(UTC) - last
        if elapsed < NUDGE_COOLDOWN:
            remaining = NUDGE_COOLDOWN - elapsed
            return {
                "allowed": False,
                "reason": f"cooldown active, {remaining} remaining",
            }

    # Grace period — never nudge before the author has had a fair chance to
    # respond. registered_at ~= when our review was posted; a nudge 43 min after
    # a review reads as pressure, not a check-in.
    if pr.registered_at:
        try:
            registered = datetime.fromisoformat(pr.registered_at)
            since_registered = datetime.now(UTC) - registered
            if since_registered < NUDGE_COOLDOWN:
                return {
                    "allowed": False,
                    "reason": f"grace period — registered {since_registered} ago",
                }
        except ValueError:
            pass

    activity_block = _nudge_activity_check(pr_number, pr.repo)
    if activity_block is not None:
        return activity_block

    if pr.last_nudge_at is None:
        return {"allowed": True, "reason": "never nudged"}
    last_nudge = datetime.fromisoformat(pr.last_nudge_at)
    elapsed_nudge = datetime.now(UTC) - last_nudge
    return {"allowed": True, "reason": f"last nudge was {elapsed_nudge} ago"}


def cmd_record_nudge(pr_number: int, repo: str) -> None:
    """Record that a nudge was actually sent for the given PR right now.

    Called by the Desktop schedule at *drain* time — never by the cron at
    enqueue time. Advances the 24h cooldown and increments ``nudge_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_record_nudge: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_nudge_at = datetime.now(UTC).isoformat()
    pr.nudge_count += 1
    save_state(state, repo)


def cmd_mark_notified(pr_number: int, repo: str, state_value: str) -> None:
    """Record that a local ping has fired for *state_value* on this PR right now."""
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_mark_notified: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_notified_state = state_value
    pr.last_notified_at = datetime.now(UTC).isoformat()
    save_state(state, repo)


def cmd_mark_escalated(pr_number: int, repo: str) -> None:
    """Record that a DM escalation was actually sent for this PR right now.

    Called by the Desktop schedule at *drain* time. Advances the escalation
    cooldown and increments ``escalation_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        logger.warning("cmd_mark_escalated: %r not found in monitored", key)
        return
    pr = state.monitored[key]
    pr.last_escalated_at = datetime.now(UTC).isoformat()
    pr.escalation_count += 1
    save_state(state, repo)


# Shared handoff directory for ship-it → review-monitor file-drop registrations.
# Located in /tmp so macOS auto-purges it on machines without the monitor (3d).
PENDING_INBOX_DIR = Path("/tmp/review-monitor/pending")  # noqa: S108  # cross-process contract: ship-it.md + cron hardcode this literal path

# Purge pending files older than this even if they couldn't be consumed.
PENDING_STALE_AFTER = timedelta(hours=24)

# Outbound-action queue drained by the Claude Desktop schedule. The cron monitor
# performs only review-based actions itself (approve, delta-review post); every
# external message — nudge, channel bump, DM escalation, cron-failure alert — is
# written here as one JSON file for Desktop to pick up and send.
DESKTOP_QUEUE_DIR = desktop_queue_dir()
DESKTOP_ACTION_TYPES = frozenset(
    {"nudge", "channel_bump", "dm_escalation", "cron_failure"}
)
# Per-PR actions get one file per (repo, pr); the rest are batched singletons.
_PER_PR_ACTIONS = frozenset({"nudge", "dm_escalation"})


def cmd_consume_pending() -> dict[str, Any]:
    """Scan the pending-inbox directory and register each PR via cmd_register.

    Returns a summary dict:
      {"consumed": [...keys...], "skipped": [...filenames...],
      "purged": [...filenames...]}

    Successfully-registered files are deleted. Files that fail validation are
    kept until PENDING_STALE_AFTER and then purged without registering.
    """
    summary: dict[str, list[str]] = {"consumed": [], "skipped": [], "purged": []}
    if not PENDING_INBOX_DIR.exists():
        return summary

    now = datetime.now(UTC)
    for path in sorted(PENDING_INBOX_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("cmd_consume_pending: cannot read %s: %s", path, e)
            if _file_is_stale(path, now):
                path.unlink(missing_ok=True)
                summary["purged"].append(path.name)
            else:
                summary["skipped"].append(path.name)
            continue

        try:
            pr_number = int(data["pr"])
            repo = str(data["repo"])
            slack_channel = str(data["slack_channel"])
            slack_ts = str(data["slack_ts"])
            sha = str(data.get("sha", ""))
            repo_path = str(data.get("repo_path", ""))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("cmd_consume_pending: invalid payload in %s: %s", path, e)
            if _file_is_stale(path, now):
                path.unlink(missing_ok=True)
                summary["purged"].append(path.name)
            else:
                summary["skipped"].append(path.name)
            continue

        cmd_register(
            pr_number=pr_number,
            role="author",
            repo=repo,
            repo_path=repo_path,
            sha=sha,
            slack_channel=slack_channel,
            slack_ts=slack_ts,
        )
        path.unlink(missing_ok=True)
        summary["consumed"].append(f"{repo}#{pr_number}")
    return summary


def _file_is_stale(path: Path, now: datetime) -> bool:
    """Return True when *path*'s mtime is older than PENDING_STALE_AFTER."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return now - mtime >= PENDING_STALE_AFTER


def _desktop_queue_filename(
    action: str, repo: str | None, pr_number: int | None
) -> str:
    """Deterministic filename — one pending file per (repo, pr, action).

    Per-PR actions key on repo+pr; batched/singleton actions (channel_bump,
    cron_failure) get a single fixed name. Deterministic names mean a re-enqueue
    refreshes the existing file rather than piling up one per cron cycle.
    """
    if action in _PER_PR_ACTIONS:
        repo_slug = (repo or "").replace("/", "_")
        return f"{action}-{repo_slug}-{pr_number}.json"
    return f"{action}.json"


def cmd_enqueue_action(
    action: str,
    payload: dict[str, Any],
    repo: str | None = None,
    pr_number: int | None = None,
) -> dict[str, Any]:
    """Write (or refresh) one outbound-action file in the Desktop action queue.

    The cron monitor sends nothing externally — it enqueues here for the Claude
    Desktop schedule to drain. Filenames are deterministic per (repo, pr,
    action), so re-enqueuing the same action each cycle refreshes the payload
    rather than piling up duplicates. ``queued_at`` is preserved across
    refreshes; ``sent_at`` stays ``null`` until the consumer drains it. The
    real cooldowns advance only when the consumer calls ``record-*`` at drain.
    """
    if action not in DESKTOP_ACTION_TYPES:
        msg = (
            f"unknown action type {action!r};"
            f" expected one of {sorted(DESKTOP_ACTION_TYPES)}"
        )
        raise ValueError(msg)
    if action in _PER_PR_ACTIONS and (not repo or pr_number is None):
        msg = f"action {action!r} requires both --repo and --pr"
        raise ValueError(msg)

    DESKTOP_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = DESKTOP_QUEUE_DIR / _desktop_queue_filename(action, repo, pr_number)
    now = datetime.now(UTC).isoformat()

    # Preserve the original queued_at across cycle refreshes.
    queued_at = now
    if path.exists():
        try:
            queued_at = json.loads(path.read_text()).get("queued_at", now)
        except (json.JSONDecodeError, OSError):
            queued_at = now

    entry = {
        "action": action,
        "repo": repo,
        "pr_number": pr_number,
        "queued_at": queued_at,
        "refreshed_at": now,
        "sent_at": None,
        "payload": payload,
    }
    # Atomic write so a mid-cycle refresh never hands the consumer a torn file.
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2))
    tmp.replace(path)
    return {"enqueued": str(path), "action": action, "queued_at": queued_at}


def cmd_catchup() -> dict[str, list[str]]:
    """Mark every currently-attention-required author PR as already notified.

    Used after a fresh deploy to avoid a first-cycle ping burst on the existing
    backlog. Does NOT fire any notifications; only updates state. The repo-less
    signature scans every state file in the central directory.

    Returns {"marked": [list of "<repo>#<pr>" keys updated]}.
    """
    marked: list[str] = []
    now_iso = datetime.now(UTC).isoformat()
    for repo in cmd_list_repos():
        state = load_state(repo)
        dirty = False
        for key, pr in state.monitored.items():
            if pr.role != "author":
                continue
            if pr.last_notified_state is not None:
                continue
            attention = _compute_attention_state(
                role=pr.role,
                status=pr.status,
                ci_ok=True,  # we don't have fresh check data; conservative assumption
                merge_blocked=False,
            )
            if attention is None:
                continue
            pr.last_notified_state = attention
            pr.last_notified_at = now_iso
            marked.append(key)
            dirty = True
        if dirty:
            save_state(state, repo)
    return {"marked": marked}


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


def cmd_status(repo: str, as_json: bool = False) -> None:
    """Print the current monitor state.

    If *as_json* is True, print the full state as JSON.
    Otherwise print a human-readable table.
    """
    state = load_state(repo)
    if as_json:
        print(json.dumps(state.to_dict(), indent=2))
        return

    if not state.monitored:
        print("No PRs currently monitored.")
    else:
        print(f"{'PR':<20} {'ROLE':<10} {'STATUS':<12} {'THREADS':<10} {'REVIEW'}")
        print("-" * 70)
        for key, pr in sorted(state.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            review_col = "re-review" if pr.awaiting_rereview else ""
            print(
                f"{key:<20} {pr.role:<10} {pr.status:<12}"
                f" {threads_col:<10} {review_col}"
            )

    print(f"\n{len(state.completed)} completed PR(s) in history.")


def cmd_status_all() -> MonitorState:
    """Load and merge state from all repo files in the central directory."""
    repos = cmd_list_repos()
    combined = MonitorState(monitored={}, completed={})
    for repo in repos:
        state = load_state(repo)
        combined.monitored.update(state.monitored)
        combined.completed.update(state.completed)
    return combined


# ---------------------------------------------------------------------------
# Auto-discover, auto-fix tracking, channel-bump
# ---------------------------------------------------------------------------


def cmd_discover(repo: str, days: int, repo_path: str) -> dict[str, Any]:
    """Find open PRs authored by the current user in *repo* and register them
    as author-role.

    Already-monitored PRs are skipped (idempotent).
    Returns ``{registered, skipped, errors}``.
    """
    state = load_state(repo)
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _run_gh(
        [
            "search",
            "prs",
            "--repo",
            repo,
            "--author",
            "@me",
            "--state",
            "open",
            "--created",
            f">={since}",
            "--json",
            "number,title",
            "--limit",
            "100",
        ],
    )
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        prs = []

    registered: list[int] = []
    skipped: list[int] = []
    for pr_info in prs:
        pr_number = pr_info.get("number")
        if not isinstance(pr_number, int):
            continue
        key = f"{repo}#{pr_number}"
        if key in state.monitored:
            skipped.append(pr_number)
            continue
        sha_raw = _run_gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            repo=repo,
        )
        sha = sha_raw.strip()
        cmd_register(
            pr_number=pr_number,
            role="author",
            repo=repo,
            repo_path=repo_path,
            sha=sha,
            review_id=None,
            threads=[],
            thread_details=None,
            slack_channel=None,
            slack_ts=None,
        )
        registered.append(pr_number)
    return {"registered": registered, "skipped": skipped, "repo": repo}


def _search_reviewed_prs(repo: str, days: int, our_username: str) -> list[int]:
    """Return numbers of open PRs the current user reviewed in the past *days*.

    PRs authored by the current user are excluded — you cannot be a reviewer on
    your own PR, and ``gh search --reviewed-by`` can still surface them (e.g. a
    review left before authorship changed). Those belong to ``discover``, not
    here.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _run_gh(
        [
            "search",
            "prs",
            "--repo",
            repo,
            "--reviewed-by",
            "@me",
            "--state",
            "open",
            "--updated",
            f">={since}",
            "--json",
            "number,author",
            "--limit",
            "100",
        ],
    )
    try:
        prs: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    return [
        p["number"]
        for p in prs
        if isinstance(p.get("number"), int)
        and (p.get("author") or {}).get("login") != our_username
    ]


def _our_reviews(pr_number: int, repo: str, our_username: str) -> list[dict[str, Any]]:
    """Return the current user's reviews on a PR, oldest-first."""
    raw = _run_gh(
        [
            "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--jq",
            f'[.[] | select(.user.login=="{our_username}")]',
        ],
    )
    try:
        result: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    return result


def _our_unresolved_threads(
    pr_number: int, repo: str, our_username: str
) -> list[dict[str, Any]]:
    """Return unresolved review threads on a PR whose first comment is the
    current user's.

    Resolved threads, and threads opened by other reviewers, are excluded — only
    our own still-open review points are returned.
    """
    raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=repo)
    try:
        data: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for thread in data.get("threads") or []:
        tid = thread.get("id", "")
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not tid or not comments:
            continue
        if thread.get("isResolved", False):
            continue
        if _extract_login(comments[0]) != our_username:
            continue
        out.append(thread)
    return out


def _recover_one_review(
    pr_number: int, repo: str, repo_path: str, our_username: str
) -> str:
    """Register one reviewed-but-unmonitored PR if it still needs watching.

    Returns the verdict ``"recovered"``, ``"already_approved"``, or
    ``"no_open_threads"``.
    """
    our_reviews = _our_reviews(pr_number, repo, our_username)
    if our_reviews and our_reviews[-1].get("state") == "APPROVED":
        # We already signed off — a lingering unresolved thread is the author's
        # to close, not a reason to re-open monitoring.
        return "already_approved"

    open_threads = _our_unresolved_threads(pr_number, repo, our_username)
    if not open_threads:
        return "no_open_threads"

    # Register at the SHA of our most recent review so the author's later
    # commits surface as a delta. Fall back to current HEAD if the review
    # commit can't be resolved (delta detection is then a no-op until the
    # next push, which is still correct).
    sha = (our_reviews[-1].get("commit_id") or "").strip() if our_reviews else ""
    if not sha:
        sha = _run_gh(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            repo=repo,
        ).strip()

    cmd_register(
        pr_number=pr_number,
        role="reviewer",
        repo=repo,
        repo_path=repo_path,
        sha=sha,
        review_id=None,
        threads=[t["id"] for t in open_threads],
        thread_details=[
            {"id": t["id"], "file": t.get("path", ""), "line": t.get("line")}
            for t in open_threads
        ],
        slack_channel=None,
        slack_ts=None,
    )
    return "recovered"


def cmd_recover_reviews(repo: str, days: int, repo_path: str) -> dict[str, Any]:
    """Recover open PRs the current user reviewed but never registered for monitoring.

    Detects the "review left, register skipped" failure mode: a delta review,
    ship-it review, or manual review posted threads on a PR, but the PR never
    entered the monitor — the register call crashed, was skipped, or the review
    predates monitoring. Without recovery those PRs go un-tracked: no delta
    review, no nudge, no approval-on-resolution.

    Each recovered PR is registered as reviewer-role at the SHA of our most
    recent review, so commits the author pushes afterwards surface as a delta.

    Idempotent. A PR is skipped — not recovered — when it is already monitored,
    already in the completed history, our latest review on it is an approval, or
    it has no unresolved threads of ours. In every one of those cases there is
    nothing left for the monitor to do.

    Returns ``{recovered, skipped_monitored, skipped_completed,
    skipped_already_approved, skipped_no_open_threads, repo}``.
    """
    state = load_state(repo)
    our_username = _get_our_username()
    buckets: dict[str, list[int]] = {
        "recovered": [],
        "skipped_monitored": [],
        "skipped_completed": [],
        "skipped_already_approved": [],
        "skipped_no_open_threads": [],
    }
    for pr_number in _search_reviewed_prs(repo, days, our_username):
        key = f"{repo}#{pr_number}"
        if key in state.monitored:
            buckets["skipped_monitored"].append(pr_number)
        elif key in state.completed:
            # Already approved/merged through the monitor — finished, not lost.
            buckets["skipped_completed"].append(pr_number)
        else:
            verdict = _recover_one_review(pr_number, repo, repo_path, our_username)
            bucket = "recovered" if verdict == "recovered" else f"skipped_{verdict}"
            buckets[bucket].append(pr_number)
    return {**buckets, "repo": repo}


def cmd_record_auto_fix(pr_number: int, repo: str) -> dict[str, Any]:
    """Increment the per-day auto-fix attempt counter for a PR.

    Returns ``{attempts_today, remaining, capped}``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    _reset_auto_fix_counter_if_stale(pr)
    pr.auto_fix_attempts_today += 1
    pr.last_auto_fix_at = datetime.now(UTC).isoformat()
    save_state(state, repo)
    remaining = max(0, AUTO_FIX_DAILY_CAP - pr.auto_fix_attempts_today)
    return {
        "attempts_today": pr.auto_fix_attempts_today,
        "remaining": remaining,
        "capped": pr.auto_fix_attempts_today >= AUTO_FIX_DAILY_CAP,
    }


def cmd_record_channel_bump(pr_number: int, repo: str) -> dict[str, Any]:
    """Record that a stale-review channel bump was actually sent for this PR.

    Called by the Desktop schedule at *drain* time. Advances the 24h bump
    cooldown and increments ``channel_bump_count``.
    """
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    if key not in state.monitored:
        return {"error": f"PR {key} not found in monitored"}
    pr = state.monitored[key]
    pr.last_channel_bump_at = datetime.now(UTC).isoformat()
    pr.channel_bump_count += 1
    save_state(state, repo)
    return {
        "last_channel_bump_at": pr.last_channel_bump_at,
        "channel_bump_count": pr.channel_bump_count,
    }


def cmd_pending_channel_bumps() -> list[dict[str, Any]]:
    """Across all repos, return author PRs whose state warrants a channel bump now.

    Each entry: ``{repo, pr_number, business_minutes_in_state, last_channel_bump_at}``.
    The caller must have run ``check`` against each monitored PR first this cycle —
    this command reads the persisted ``state_entered_at`` and ``status`` fields only.
    """
    pending: list[dict[str, Any]] = []
    for repo in cmd_list_repos():
        state = load_state(repo)
        for _, pr in sorted(state.monitored.items()):
            if pr.role != "author":
                continue
            if pr.status != "ready_to_approve":
                continue
            if not _needs_channel_bump(pr, "ready_to_approve"):
                continue
            pending.append(
                {
                    "repo": pr.repo,
                    "pr_number": pr.pr_number,
                    "business_minutes_in_state": _business_minutes_in_state(pr),
                    "last_channel_bump_at": pr.last_channel_bump_at,
                }
            )
    return pending


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _add_lifecycle_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that create or change a PR's monitored lifecycle."""
    p_reg = subparsers.add_parser("register", help="Start monitoring a PR")
    p_reg.add_argument("pr_number", type=int)
    p_reg.add_argument("--role", required=True, choices=["reviewer", "author"])
    p_reg.add_argument("--repo", required=True)
    p_reg.add_argument("--repo-path", required=True)
    p_reg.add_argument("--sha", required=True)
    p_reg.add_argument("--review-id")
    p_reg.add_argument("--threads", nargs="*", default=[])
    p_reg.add_argument("--thread-details", help="JSON list of {id,file,line} objects")
    p_reg.add_argument(
        "--slack-channel", help="Slack channel ID for PR announcement thread"
    )
    p_reg.add_argument("--slack-ts", help="Parent ts for PR announcement thread")

    p_drop = subparsers.add_parser("drop", help="Stop monitoring a PR")
    p_drop.add_argument("pr_number", type=int)
    p_drop.add_argument("--repo", required=True)

    p_complete = subparsers.add_parser("complete", help="Mark a PR as done")
    p_complete.add_argument("pr_number", type=int)
    p_complete.add_argument("--repo", required=True)
    p_complete.add_argument("--reason", default="merged")

    p_set_status = subparsers.add_parser(
        "set-status", help="Set lifecycle status for a PR"
    )
    p_set_status.add_argument("pr_number", type=int)
    p_set_status.add_argument("--repo", required=True)
    p_set_status.add_argument(
        "--status",
        required=True,
        choices=["watching", "ready_to_approve", "approved"],
    )

    p_confirm_thread = subparsers.add_parser(
        "confirm-thread",
        help="Mark a thread addressed-by-code-change (delta-review confirmation pass)",
    )
    p_confirm_thread.add_argument("pr_number", type=int)
    p_confirm_thread.add_argument("--repo", required=True)
    p_confirm_thread.add_argument("--thread", required=True, dest="thread_id")

    p_ack_delta = subparsers.add_parser(
        "ack-delta",
        help=(
            "Acknowledge the reviewer delta through --sha has been processed"
            " (Step 3 close)"
        ),
    )
    p_ack_delta.add_argument("pr_number", type=int)
    p_ack_delta.add_argument("--repo", required=True)
    p_ack_delta.add_argument("--sha", required=True)


def _add_signal_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that record nudges, notifications, and cursors."""
    p_nudge_ok = subparsers.add_parser("nudge-ok", help="Check if a nudge is allowed")
    p_nudge_ok.add_argument("pr_number", type=int)
    p_nudge_ok.add_argument("--repo", required=True)

    p_record_nudge = subparsers.add_parser(
        "record-nudge", help="Record a nudge was sent"
    )
    p_record_nudge.add_argument("pr_number", type=int)
    p_record_nudge.add_argument("--repo", required=True)

    p_mark_cr = subparsers.add_parser(
        "mark-comment-review",
        help="Record the classifier's verdict on a tracked COMMENTED review",
    )
    p_mark_cr.add_argument("pr_number", type=int)
    p_mark_cr.add_argument("--repo", required=True)
    p_mark_cr.add_argument("--review-id", required=True, dest="review_id")
    p_mark_cr.add_argument(
        "--classification",
        required=True,
        choices=["requests_changes", "neutral"],
    )

    p_mark_notified = subparsers.add_parser(
        "mark-notified", help="Record that a local ping fired for a state"
    )
    p_mark_notified.add_argument("pr_number", type=int)
    p_mark_notified.add_argument("--repo", required=True)
    p_mark_notified.add_argument("--state", required=True, dest="state_value")

    p_mark_escalated = subparsers.add_parser(
        "mark-escalated", help="Record that a Slack-bot escalation fired"
    )
    p_mark_escalated.add_argument("pr_number", type=int)
    p_mark_escalated.add_argument("--repo", required=True)

    p_slack_cursor = subparsers.add_parser(
        "slack-thread-cursor", help="Print Slack channel+ts+last_seen for a PR"
    )
    p_slack_cursor.add_argument("pr_number", type=int)
    p_slack_cursor.add_argument("--repo", required=True)

    p_update_cursor = subparsers.add_parser(
        "update-slack-cursor", help="Advance the slack_last_seen_ts cursor"
    )
    p_update_cursor.add_argument("pr_number", type=int)
    p_update_cursor.add_argument("--repo", required=True)
    p_update_cursor.add_argument("--last-seen-ts", required=True)

    p_record_fix = subparsers.add_parser(
        "record-auto-fix",
        help="Increment the per-day auto-fix attempt counter for a PR",
    )
    p_record_fix.add_argument("pr_number", type=int)
    p_record_fix.add_argument("--repo", required=True)

    p_record_bump = subparsers.add_parser(
        "record-channel-bump",
        help="Record that a stale-review channel bump was posted for a PR",
    )
    p_record_bump.add_argument("pr_number", type=int)
    p_record_bump.add_argument("--repo", required=True)


def _add_query_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register read-only query, discovery, and queue subcommands."""
    p_status = subparsers.add_parser("status", help="Show current monitor state")
    p_status.add_argument("--repo")
    p_status.add_argument("--all", dest="all_repos", action="store_true")
    p_status.add_argument("--json", dest="as_json", action="store_true")

    p_list = subparsers.add_parser("list-repos", help="List repos with state files")
    p_list.add_argument("--json", dest="as_json", action="store_true")

    p_check = subparsers.add_parser("check", help="Run one monitoring cycle for a PR")
    p_check.add_argument("pr_number", type=int)
    p_check.add_argument("--repo", required=True)

    subparsers.add_parser(
        "consume-pending",
        help="Scan /tmp/review-monitor/pending/ and register any PRs found",
    )
    subparsers.add_parser(
        "catchup",
        help=(
            "Mark every existing author-role attention PR as already notified"
            " (no pings fired)"
        ),
    )
    subparsers.add_parser(
        "pending-channel-bumps",
        help="Across all repos, list author PRs needing a stale-review channel bump",
    )

    p_discover = subparsers.add_parser(
        "discover",
        help="Auto-register open author PRs from the past N days for a repo",
    )
    p_discover.add_argument("--repo", required=True)
    p_discover.add_argument("--repo-path", required=True)
    p_discover.add_argument("--days", type=int, default=7)

    p_recover = subparsers.add_parser(
        "recover-reviews",
        help=(
            "Auto-register open PRs reviewed by you in the past N days"
            " that were never monitored"
        ),
    )
    p_recover.add_argument("--repo", required=True)
    p_recover.add_argument("--repo-path", required=True)
    p_recover.add_argument("--days", type=int, default=7)

    p_enqueue = subparsers.add_parser(
        "enqueue-action",
        help="Write one outbound action to the Desktop action queue",
    )
    p_enqueue.add_argument(
        "--type",
        required=True,
        dest="action_type",
        choices=sorted(DESKTOP_ACTION_TYPES),
    )
    p_enqueue.add_argument("--repo")
    p_enqueue.add_argument("--pr", type=int, dest="pr_number")
    p_enqueue.add_argument(
        "--payload", required=True, help="JSON object with the action's message data"
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        description="Review monitor: track PR review threads",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_lifecycle_subparsers(subparsers)
    _add_signal_subparsers(subparsers)
    _add_query_subparsers(subparsers)
    return parser


def _dispatch_status_all(as_json: bool) -> None:
    """Print merged status across all repos."""
    combined = cmd_status_all()
    if as_json:
        print(json.dumps(combined.to_dict(), indent=2))
        return
    if not combined.monitored:
        print("No PRs currently monitored across any repo.")
    else:
        print(f"{'PR':<40} {'ROLE':<10} {'STATUS':<12} {'THREADS ADDRESSED'}")
        print("-" * 80)
        for key, pr in sorted(combined.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            print(f"{key:<40} {pr.role:<10} {pr.status:<12} {threads_col}")
    print(f"\n{len(combined.completed)} completed PR(s) in history.")


def _dispatch_pr_state_mutation(args: argparse.Namespace) -> None:
    """Dispatch the single-PR state mutations
    (set-status/confirm-thread/mark-*/cursor)."""
    if args.command == "set-status":
        cmd_set_status(pr_number=args.pr_number, repo=args.repo, status=args.status)
    elif args.command == "confirm-thread":
        cmd_confirm_thread(
            pr_number=args.pr_number, repo=args.repo, thread_id=args.thread_id
        )
    elif args.command == "mark-notified":
        cmd_mark_notified(
            pr_number=args.pr_number, repo=args.repo, state_value=args.state_value
        )
    elif args.command == "mark-escalated":
        cmd_mark_escalated(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "update-slack-cursor":
        cmd_update_slack_cursor(
            pr_number=args.pr_number, repo=args.repo, last_seen_ts=args.last_seen_ts
        )


def _dispatch_discovery(args: argparse.Namespace) -> None:
    """Dispatch the repo-wide PR discovery commands (discover / recover-reviews)."""
    if args.command == "discover":
        result = cmd_discover(repo=args.repo, days=args.days, repo_path=args.repo_path)
    else:
        result = cmd_recover_reviews(
            repo=args.repo, days=args.days, repo_path=args.repo_path
        )
    print(json.dumps(result, indent=2))


def _emit_mutation_result(label: str, run: Callable[[], dict[str, Any]]) -> None:
    """Run a state-mutating command and print its one-line JSON result.

    A state read/write failure (``OSError``) or malformed JSON in an argument
    (``json.JSONDecodeError``) becomes an ``Error:`` line on stderr and exit 1 —
    never a silent success or a traceback — so the caller learns from the
    command itself whether the mutation landed.
    """
    try:
        result = run()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: {label} failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result))


def _dispatch_mutation(args: argparse.Namespace) -> None:
    """Dispatch register/drop/complete/nudge-ok/record-nudge/set-status/
    confirm-thread commands."""
    label = f"{args.command} {args.repo}#{args.pr_number}"
    if args.command == "register":
        _emit_mutation_result(
            label,
            lambda: cmd_register(
                pr_number=args.pr_number,
                role=args.role,
                repo=args.repo,
                repo_path=args.repo_path,
                sha=args.sha,
                review_id=args.review_id,
                threads=args.threads,
                thread_details=(
                    json.loads(args.thread_details) if args.thread_details else None
                ),
                slack_channel=args.slack_channel,
                slack_ts=args.slack_ts,
            ),
        )
    elif args.command == "drop":
        _emit_mutation_result(
            label, lambda: cmd_drop(pr_number=args.pr_number, repo=args.repo)
        )
    elif args.command == "complete":
        _emit_mutation_result(
            label,
            lambda: cmd_complete(
                pr_number=args.pr_number, repo=args.repo, reason=args.reason
            ),
        )
    elif args.command == "nudge-ok":
        print(
            json.dumps(cmd_nudge_ok(pr_number=args.pr_number, repo=args.repo), indent=2)
        )
    elif args.command == "record-nudge":
        cmd_record_nudge(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "enqueue-action":
        result = cmd_enqueue_action(
            action=args.action_type,
            payload=json.loads(args.payload),
            repo=args.repo,
            pr_number=args.pr_number,
        )
        print(json.dumps(result, indent=2))
    else:
        _dispatch_pr_state_mutation(args)


_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "register",
        "drop",
        "complete",
        "nudge-ok",
        "record-nudge",
        "enqueue-action",
        "set-status",
        "confirm-thread",
        "mark-notified",
        "mark-escalated",
        "update-slack-cursor",
    }
)

# Query commands: each maps to a callable returning a JSON-serializable result
# that main() prints with indent=2. list-repos and status branch on flags and
# are handled separately.
_QUERY_COMMANDS: dict[str, Callable[[argparse.Namespace], Any]] = {
    "check": lambda a: cmd_check(pr_number=a.pr_number, repo=a.repo),
    "slack-thread-cursor": lambda a: cmd_slack_thread_cursor(
        pr_number=a.pr_number, repo=a.repo
    ),
    "consume-pending": lambda _: cmd_consume_pending(),
    "record-auto-fix": lambda a: cmd_record_auto_fix(
        pr_number=a.pr_number, repo=a.repo
    ),
    "record-channel-bump": lambda a: cmd_record_channel_bump(
        pr_number=a.pr_number, repo=a.repo
    ),
    "pending-channel-bumps": lambda _: cmd_pending_channel_bumps(),
    "mark-comment-review": lambda a: cmd_mark_comment_review(
        pr_number=a.pr_number,
        repo=a.repo,
        review_id=a.review_id,
        classification=a.classification,
    ),
    "catchup": lambda _: cmd_catchup(),
    "ack-delta": lambda a: cmd_ack_delta(pr_number=a.pr_number, repo=a.repo, sha=a.sha),
}


def _print_repo_list(*, as_json: bool) -> None:
    """Print the monitored-repo list as JSON or newline-separated names."""
    repos = cmd_list_repos()
    if as_json:
        print(json.dumps(repos))
    else:
        for r in repos:
            print(r)


def _dispatch_status_command(args: argparse.Namespace) -> None:
    """Run the ``status`` command for one repo or all repos.

    When neither ``--repo`` nor ``--all`` is supplied, auto-derive ``--repo``
    from ``gh repo view`` against the current working directory so callers
    inside a checkout don't have to pass it explicitly.
    """
    if args.all_repos:
        _dispatch_status_all(as_json=args.as_json)
        return
    repo = args.repo or _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if not repo:
        print(
            "Error: --repo is required when --all is not set "
            "(auto-derive via `gh repo view` failed — run inside a checkout "
            "or pass --repo owner/name)",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd_status(repo=repo, as_json=args.as_json)


def main() -> None:
    """CLI entry point."""
    parser = _build_argument_parser()
    args = parser.parse_args()
    command: str | None = args.command

    if command in _MUTATION_COMMANDS:
        _dispatch_mutation(args)
    elif command in ("discover", "recover-reviews"):
        _dispatch_discovery(args)
    elif command == "list-repos":
        _print_repo_list(as_json=args.as_json)
    elif command == "status":
        _dispatch_status_command(args)
    else:
        handler = _QUERY_COMMANDS.get(command or "")
        if handler is not None:
            print(json.dumps(handler(args), indent=2))


if __name__ == "__main__":
    main()
