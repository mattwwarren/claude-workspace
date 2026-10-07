"""Nudge, notification, escalation and channel-bump bookkeeping subcommands."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from review_monitor_lib.attention import _compute_attention_state
from review_monitor_lib.escalation import (
    AUTO_FIX_DAILY_CAP,
    _business_minutes_in_state,
    _needs_channel_bump,
    _reset_auto_fix_counter_if_stale,
)
from review_monitor_lib.shell import _run_gh
from review_monitor_lib.state import cmd_list_repos, load_state, save_state

logger = logging.getLogger(__name__)


# Minimum time between nudge messages for the same PR
NUDGE_COOLDOWN = timedelta(hours=24)


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
