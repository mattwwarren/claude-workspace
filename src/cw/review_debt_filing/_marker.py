"""The review-debt body markers and the dedup match (#3041)."""

from __future__ import annotations

from cw.review_debt_filing._constants import (
    DEBT_MARKER_PREFIX,
    MARKER_SUFFIX,
    PR_MARKER_PREFIX,
)


def debt_marker(key: str) -> str:
    """``<!-- cw-review-debt: <key> -->``, the dedup recovery marker."""
    return f"{DEBT_MARKER_PREFIX}{key}{MARKER_SUFFIX}"


def pr_marker(pr_url: str) -> str:
    """``<!-- cw-review-debt-pr: <url> -->``, the PR link III2 (#3044) parses."""
    return f"{PR_MARKER_PREFIX}{pr_url}{MARKER_SUFFIX}"


def scope_prefix(ticket_ref: str) -> str:
    """The first-line prefix that scopes a debt body to one queue ticket."""
    return f"Deferred during review of {ticket_ref} on branch"


def body_matches(body: str, key: str, ticket_ref: str) -> bool:
    """True when *body* is the debt ticket for *key* under *ticket_ref*.

    Both must hold: the body carries the key marker on a line of its own, and
    its first line names the same queue ticket. The same key deferred under a
    different queue ticket is a miss (dedup scope is the queue ticket).
    """
    lines = body.splitlines()
    if not lines or not lines[0].startswith(scope_prefix(ticket_ref)):
        return False
    marker = debt_marker(key)
    return any(line.strip() == marker for line in lines)


def has_pr_marker(body: str, pr_url: str) -> bool:
    """True when *body* already links *pr_url*."""
    marker = pr_marker(pr_url)
    return any(line.strip() == marker for line in body.splitlines())


def with_pr_marker(body: str, pr_url: str) -> str:
    """*body* with the PR marker line appended."""
    return f"{body.rstrip()}\n{pr_marker(pr_url)}\n"
