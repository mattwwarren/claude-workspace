"""PR-URL and git-remote slug parsing (pure, no git).

Part of the ``cw.pr_hydrate`` package (#2999). The git-running
``_resolve_repo_slug`` stays in the package ``__init__``, where the suite
patches ``run_git``.
"""

from __future__ import annotations

import re

_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")
# GitHub #1198 — parse an ``owner/repo`` slug out of a git remote URL, covering
# both SSH (git@github.com:owner/repo.git) and HTTPS
# (https://github.com/owner/repo[.git]) forms. Non-github remotes don't match.
_REMOTE_SLUG_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")


def _parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse ``(owner/repo, pr_number)`` from a GitHub PR URL, or None."""
    match = _PR_URL_RE.search(pr_url)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _slug_mismatch(pr_repo: str, resolved: str | None) -> str | None:
    """Return *resolved* when it disagrees with *pr_repo*, else None. No git.

    Fail-open (GitHub #1198): an unresolvable remote (``None``) is no mismatch,
    never *pr_repo*. Case-insensitive, so a remote's casing never trips it.
    """
    if resolved is None or resolved.lower() == pr_repo.lower():
        return None
    return resolved
