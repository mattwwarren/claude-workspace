"""The review-debt body markers, the dedup match and their parsers (#3041, #3044)."""

from __future__ import annotations

import re

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


def _marker_values(body: str, prefix: str) -> list[str]:
    """The inner text of every *prefix* marker on a line of its own, deduped.

    A line counts only when, stripped, it starts with *prefix* and ends with
    :data:`MARKER_SUFFIX`, so a marker quoted inside prose never matches. An
    empty inner value is skipped. First occurrence wins; order is kept.
    """
    values: list[str] = []
    shortest = len(prefix) + len(MARKER_SUFFIX)
    for raw in body.splitlines():
        line = raw.strip()
        if len(line) < shortest:
            continue
        if not (line.startswith(prefix) and line.endswith(MARKER_SUFFIX)):
            continue
        value = line[len(prefix) : len(line) - len(MARKER_SUFFIX)].strip()
        if value and value not in values:
            values.append(value)
    return values


def parse_pr_markers(body: str) -> list[str]:
    """Every PR URL *body* links through a PR marker, in body order (#3044).

    The URLs are returned as written; the caller validates their shape.
    """
    return _marker_values(body, PR_MARKER_PREFIX)


def parse_debt_key(body: str) -> str | None:
    """The debt key from *body*'s key marker, or None when it does not parse."""
    keys = _marker_values(body, DEBT_MARKER_PREFIX)
    return keys[0] if keys else None


_TICKET_SENTINEL = "\x00"
_SCOPE_HEAD, _SCOPE_TAIL = scope_prefix(_TICKET_SENTINEL).split(_TICKET_SENTINEL)
_GITHUB_SCOPE_RE = re.compile(
    rf"{re.escape(_SCOPE_HEAD)}#([1-9][0-9]*){re.escape(_SCOPE_TAIL)}"
)


def queue_ticket_ref(body: str) -> str | None:
    """The GitHub queue ticket number *body*'s first line names, or None.

    Derived from :func:`scope_prefix`, so the writer and this reader cannot
    drift. ``Deferred during review of #3041 on branch ...`` yields ``"3041"``.
    """
    lines = body.splitlines()
    match = _GITHUB_SCOPE_RE.match(lines[0]) if lines else None
    return match.group(1) if match is not None else None
