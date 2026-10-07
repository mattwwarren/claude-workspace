"""Auto-discovery of authored PRs and recovery of reviewed PRs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from review_monitor_lib.lifecycle import cmd_register
from review_monitor_lib.shell import _get_our_username, _run_gh
from review_monitor_lib.state import load_state
from review_monitor_lib.threads import _extract_login


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
