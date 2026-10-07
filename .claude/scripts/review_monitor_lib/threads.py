"""Review-thread discovery, deferral detection and status transitions."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from review_monitor_lib.models import ThreadStatus
from review_monitor_lib.shell import _get_our_username, _run_gh

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR


# Patterns in review comments that indicate the author is deferring work
DEFERRAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfollow[\s-]?up\b", re.IGNORECASE),
    re.compile(r"\bseparate\s+pr\b", re.IGNORECASE),
    re.compile(r"\bout\s+of\s+scope\b", re.IGNORECASE),
    re.compile(r"\bnext\s+sprint\b", re.IGNORECASE),
    re.compile(r"\bwill\s+address\s+later\b", re.IGNORECASE),
    re.compile(r"\btracking\s+in\b", re.IGNORECASE),
]


def is_deferral(text: str) -> bool:
    """Return True if *text* contains deferral language.

    Checks against all patterns in :data:`DEFERRAL_PATTERNS`.
    """
    return any(pat.search(text) for pat in DEFERRAL_PATTERNS)


def _extract_login(comment: dict[str, Any]) -> str:
    """Extract login from a review comment, handling both str and dict author fields."""
    author = comment.get("author", "")
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("login", ""))
    return ""


def _discover_author_threads(
    pr: MonitoredPR,
    threads: list[dict[str, Any]],
    our_username: str,
) -> None:
    """Discover threads opened by others on an author-role PR.

    Mutates *pr.our_threads* and *pr.thread_status* in place.
    """
    for thread in threads:
        tid: str = thread.get("id", "")
        if not tid:
            continue
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not comments:
            continue
        first_author: str = _extract_login(comments[0])
        if first_author != our_username and tid not in pr.our_threads:
            pr.our_threads.append(tid)
            if tid not in pr.thread_status:
                pr.thread_status[tid] = ThreadStatus(
                    file=thread.get("path", ""),
                    line=thread.get("line", 0),
                )


def _collect_deferred_threads_for_followup(
    pr: MonitoredPR, pr_number: int
) -> list[dict[str, Any]]:
    """Return deferred-thread metadata suitable for follow-up ticket creation.

    Called from ``cmd_check`` when a monitored PR transitions to MERGED.
    Looks up the deferred thread IDs tracked locally, fetches their bodies
    from GitHub once (single ``gh review view`` call), and returns one dict
    per thread for the skill to turn into a Linear ticket.

    Returns an empty list when no threads are deferred — the common case.
    """
    deferred_ids = {tid for tid, ts in pr.thread_status.items() if ts.deferred}
    if not deferred_ids:
        return []
    review_raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=pr.repo)
    try:
        review_data: dict[str, Any] = json.loads(review_raw)
    except (json.JSONDecodeError, ValueError):
        return []
    our_username = _get_our_username()
    out: list[dict[str, Any]] = []
    for thread in review_data.get("threads") or []:
        tid = thread.get("id", "")
        if tid not in deferred_ids:
            continue
        comments: list[dict[str, Any]] = thread.get("comments", [])
        if not comments:
            continue
        reviewer_comment = comments[0].get("body", "")
        reviewer_author = _extract_login(comments[0])
        deferral_reply = ""
        for c in comments[1:]:
            body = c.get("body", "")
            if _extract_login(c) == our_username and is_deferral(body):
                deferral_reply = body
                break
        out.append(
            {
                "thread_id": tid,
                "file": thread.get("path", ""),
                "line": thread.get("line", 0),
                "reviewer": reviewer_author,
                "reviewer_comment": reviewer_comment,
                "deferral_reply": deferral_reply,
                "url": comments[0].get("url", ""),
            }
        )
    return out


def _update_thread_status(
    ts: ThreadStatus,
    thread: dict[str, Any],
    role: str,
    our_username: str,
) -> None:
    """Update a single ThreadStatus from the current GitHub thread data.

    Mutates *ts* in place.
    """
    comments: list[dict[str, Any]] = thread.get("comments", [])
    ts.resolved = bool(thread.get("isResolved", False))
    ts.replied = False
    ts.deferred = False

    if len(comments) <= 1:
        return

    subsequent = comments[1:]
    if role == "reviewer":
        ts.replied = any(_extract_login(c) != our_username for c in subsequent)
    else:
        reply_by_us = [c for c in subsequent if _extract_login(c) == our_username]
        ts.replied = bool(reply_by_us)
        if ts.replied:
            ts.deferred = any(is_deferral(c.get("body", "")) for c in reply_by_us)


def _apply_status_transitions(
    pr: MonitoredPR, changed: bool, is_draft: bool = False
) -> None:
    """Apply lifecycle status transitions to *pr* based on thread state and
    commit changes.

    Transitions:
    - ``watching`` → ``ready_to_approve`` when all threads are addressed.
    - ``ready_to_approve`` or ``approved`` → ``watching`` when new commits land.
    - Any → ``watching`` when the PR is currently a draft. Drafts are
      author-controlled WIP and must never appear in the channel-bump / DM
      escalation paths, even if they previously reached ``ready_to_approve``
      before being converted back to draft.

    Mutates *pr.status* in place.
    """
    if is_draft:
        if pr.status in ("ready_to_approve", "approved"):
            pr.status = "watching"
        return
    if pr.all_threads_addressed() and pr.status == "watching":
        pr.status = "ready_to_approve"
    if changed and pr.status in ("ready_to_approve", "approved"):
        pr.status = "watching"


def _refresh_threads(
    pr: MonitoredPR, pr_number: int, our_username: str
) -> dict[str, dict[str, Any]]:
    """Fetch review threads from GitHub and update each tracked thread's status.

    For author-role PRs, also discovers new threads opened by others. Returns
    a ``{thread_id: status_dict}`` map of the tracked threads.
    """
    review_raw = _run_gh(["review", "view", str(pr_number), "--json"], repo=pr.repo)
    try:
        review_data: dict[str, Any] = json.loads(review_raw)
    except (json.JSONDecodeError, ValueError):
        review_data = {}
    threads: list[dict[str, Any]] = review_data.get("threads") or []

    if pr.role == "author":
        _discover_author_threads(pr, threads, our_username)

    thread_updates: dict[str, dict[str, Any]] = {}
    threads_by_id: dict[str, dict[str, Any]] = {t.get("id", ""): t for t in threads}
    for tid in pr.our_threads:
        thread = threads_by_id.get(tid)
        if thread is None:
            continue
        ts = pr.thread_status.setdefault(
            tid,
            ThreadStatus(file=thread.get("path", ""), line=thread.get("line", 0)),
        )
        _update_thread_status(ts, thread, pr.role, our_username)
        thread_updates[tid] = ts.to_dict()
    return thread_updates
