"""Comment-only (COMMENTED) review tracking and bot-login classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from review_monitor_lib.models import CommentReviewRef
from review_monitor_lib.shell import _run_gh
from review_monitor_lib.state import load_state, save_state

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR


# GitHub login suffixes that identify automated accounts
BOT_LOGIN_SUFFIXES: tuple[str, ...] = ("[bot]", "-ai", "-bot")
KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "sourcery-ai",
        "coderabbitai",
        "dependabot",
        "renovate",
        "github-actions",
        "codecov-commenter",
    }
)
# Bots whose findings BLOCK merge — must be treated as human-equivalent for
# auto-fix purposes even though the login matches a bot pattern.
MERGE_BLOCKING_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "sonarqubecloud",
        "sonarcloud",
        "sonarqube",
        "sonarcloud[bot]",
        "sonarqubecloud[bot]",
    }
)


def cmd_mark_comment_review(
    pr_number: int, repo: str, review_id: str, classification: str
) -> dict[str, Any]:
    """Persist the skill classifier's verdict on a tracked comment review.

    ``classification`` must be either ``"requests_changes"`` or ``"neutral"``.
    A ``requests_changes`` verdict makes ``has_actionable_comment_review`` true
    on the next ``check``, promoting ``attention_state`` to ``changes_requested``
    via the fallback branch in ``_compute_attention_state``.
    """
    if classification not in ("requests_changes", "neutral"):
        return {"error": f"invalid classification {classification!r}"}
    state = load_state(repo)
    key = f"{repo}#{pr_number}"
    pr = state.monitored.get(key)
    if pr is None:
        return {"error": f"PR {key} not found in monitored"}
    ref = pr.comment_reviews.get(review_id)
    if ref is None:
        return {"error": f"review_id {review_id} not tracked on {key}"}
    ref.classification = classification
    ref.classified_at = datetime.now(UTC).isoformat()
    save_state(state, repo)
    return {
        "ok": True,
        "pr_number": pr_number,
        "review_id": review_id,
        "classification": classification,
    }


def _refresh_comment_reviews(
    pr: MonitoredPR, repo: str, pr_number: int, sha_changed: bool, our_username: str
) -> bool:
    """Refresh ``pr.comment_reviews`` from GitHub.

    A tracked entry is a non-bot ``COMMENTED`` review submitted *after* the
    most recent state-resetting event:
      - The author pushed a new commit (``sha_changed == True``), or
      - A formal ``CHANGES_REQUESTED`` / ``APPROVED`` review landed (its
        ``submitted_at`` becomes the cutoff and stale comment-reviews drop).

    Persisted classification verdicts survive across cycles. New reviews are
    inserted with ``classification == "unclassified"`` for the skill to handle.

    Returns
    -------
        True when at least one human (non-bot, not ``our_username``) has left a
        ``COMMENTED`` or ``CHANGES_REQUESTED`` review on this PR. Why: a human
        review — formal or a bare comment — means a reviewer has already
        engaged and given feedback; once we address it they must look again
        (and for ``CHANGES_REQUESTED``, personally APPROVE, or branch
        protection blocks the merge). The caller uses this to label the PR
        "awaiting re-review" vs "awaiting first review". False on fetch error.
    """
    raw = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}/reviews"])
    try:
        reviews: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False

    if sha_changed:
        # Author pushed something — any prior comment-review concerns may have
        # been addressed by the push. Drop and re-evaluate from scratch.
        pr.comment_reviews.clear()

    formal_cutoff = ""
    for r in reviews:
        if r.get("state") in ("CHANGES_REQUESTED", "APPROVED"):
            ts = r.get("submitted_at", "") or ""
            formal_cutoff = max(formal_cutoff, ts)

    if formal_cutoff:
        pr.comment_reviews = {
            rid: ref
            for rid, ref in pr.comment_reviews.items()
            if ref.submitted_at > formal_cutoff
        }

    inline_by_review: dict[str, list[str]] = {}
    if any(
        r.get("state") == "COMMENTED"
        and (r.get("user") or {}).get("login") == our_username
        and not (r.get("body") or "").strip()
        for r in reviews
    ):
        inline_by_review = _fetch_inline_comment_bodies_by_review(repo, pr_number)

    _collect_new_comment_reviews(
        pr,
        reviews,
        formal_cutoff,
        our_username=our_username,
        inline_by_review=inline_by_review,
    )
    return _has_engaged_human_reviewer(reviews, our_username)


def _fetch_inline_comment_bodies_by_review(
    repo: str, pr_number: int
) -> dict[str, list[str]]:
    """Map ``pull_request_review_id`` to active inline-comment bodies for a PR.

    Only comments still anchored to a live diff position are included —
    GitHub sets ``line`` to ``null`` once a comment's diff position is
    outdated, and a stale thread must not resurrect an already-handled
    review's body.
    """
    raw = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}/comments"])
    try:
        comments: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    by_review: dict[str, list[str]] = {}
    for comment in comments:
        if comment.get("line") is None:
            continue
        review_id = comment.get("pull_request_review_id")
        body = (comment.get("body") or "").strip()
        if review_id is None or not body:
            continue
        by_review.setdefault(str(review_id), []).append(body)
    return by_review


def _collect_new_comment_reviews(
    pr: MonitoredPR,
    reviews: list[dict[str, Any]],
    formal_cutoff: str,
    our_username: str = "",
    inline_by_review: dict[str, list[str]] | None = None,
) -> None:
    """Insert untracked non-bot ``COMMENTED`` reviews into ``pr.comment_reviews``.

    Skips bot authors, and reviews at/before ``formal_cutoff``. A blank body
    is normally just the carrier for inline file-level comments — those are
    tracked through threads — but when the review is ours (``author ==
    our_username``) and *inline_by_review* has entries for it, the body is
    reconstructed by joining those inline comment bodies, since a
    ``COMMENTED`` review whose entire substance lives in inline comments
    would otherwise look empty and be skipped. Already-tracked reviews are
    left as-is so their persisted classification survives.
    """
    inline_by_review = inline_by_review or {}
    for r in reviews:
        if r.get("state") != "COMMENTED":
            continue
        author = (r.get("user") or {}).get("login", "")
        if not author or is_bot_login(author):
            continue
        submitted_at = r.get("submitted_at", "") or ""
        if formal_cutoff and submitted_at <= formal_cutoff:
            continue
        rid = str(r.get("id"))
        body = (r.get("body") or "").strip()
        if not body:
            if author == our_username and rid in inline_by_review:
                body = "\n\n".join(inline_by_review[rid])
            if not body:
                continue
        if rid in pr.comment_reviews:
            continue  # already tracked; preserve classification
        pr.comment_reviews[rid] = CommentReviewRef(
            review_id=rid,
            author=author,
            submitted_at=submitted_at,
            body=body[:2000],
        )


def _has_engaged_human_reviewer(
    reviews: list[dict[str, Any]], our_username: str
) -> bool:
    """Return True when a non-bot reviewer other than us left a non-APPROVED review.

    A human reviewer has engaged if anyone non-bot (other than us) left a
    ``COMMENTED`` or ``CHANGES_REQUESTED`` review. ``APPROVED`` is excluded —
    that reviewer is already satisfied and needs no further look.
    """
    return any(
        r.get("state") in ("COMMENTED", "CHANGES_REQUESTED")
        and (login := ((r.get("user") or {}).get("login") or ""))
        and login != our_username
        and not is_bot_login(login)
        for r in reviews
    )


def _collect_pending_comment_reviews(
    pr: MonitoredPR,
    *,
    unaddressed_count: int,
    review_decision: str,
    merge_blocked: bool,
    merge_state_status: str,
    ci_ok: bool,
) -> list[dict[str, Any]]:
    """Surface unclassified comment reviews — only as a fallback attention signal.

    Returned ONLY when no other signal would already trigger attention. Keeps
    the skill from burning classifier tokens on PRs with already-actionable
    inline/formal CRs. Note: merge_state_status == "BLOCKED" is NOT a
    higher-priority signal — _compute_attention_state reclassifies it as
    ready_to_approve (waiting on required reviews). Only DIRTY/BEHIND beats the
    fallback path.
    """
    code_fixable_merge_block = merge_blocked and merge_state_status in (
        "DIRTY",
        "BEHIND",
    )
    no_other_signal = (
        not code_fixable_merge_block
        and ci_ok
        and unaddressed_count == 0
        and review_decision != "CHANGES_REQUESTED"
    )
    if pr.role != "author" or not no_other_signal:
        return []
    return [
        {
            "review_id": ref.review_id,
            "author": ref.author,
            "submitted_at": ref.submitted_at,
            "body": ref.body,
        }
        for ref in pr.comment_reviews.values()
        if ref.classification == "unclassified"
    ]


def is_bot_login(login: str) -> bool:
    """Return True when *login* looks like a bot whose findings can be silently skipped.

    Bots in ``MERGE_BLOCKING_BOT_LOGINS`` (sonarqube/sonarcloud) override and return
    False — their findings block merge and must be addressed like human review threads.
    """
    if not login:
        return False
    lower = login.lower()
    if lower in MERGE_BLOCKING_BOT_LOGINS:
        return False
    if lower in KNOWN_BOT_LOGINS:
        return True
    return any(lower.endswith(suffix) for suffix in BOT_LOGIN_SUFFIXES)
