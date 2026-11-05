"""PR writes: reviewer requests and auto-merge disarm."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING

from cw.gh.runner import _POST_COMMENT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path


def add_pr_reviewer(
    pr_ref: str,
    reviewer: str,
    *,
    timeout: int = _POST_COMMENT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> _sp.CompletedProcess[bytes] | None:
    """Request *reviewer* on *pr_ref* via ``gh pr edit --add-reviewer``.

    *pr_ref* may be a full PR URL (``gh`` infers owner/repo) or a PR number;
    *reviewer* is a GitHub login or an ``org/team`` slug. Mirrors
    ``post_issue_comment``: returns the completed subprocess (inspect
    ``.returncode`` / ``.stderr``) or None if the subprocess could not run /
    timed out. Policy-free: callers decide whether to log or swallow — this
    neither logs nor raises on gh failure.

    *cwd* scopes the ``gh`` call to a client's repo (defaults to None, i.e.
    ambient CWD); multi-client callers MUST pass it (GitHub #1269/#1279).
    """
    try:
        return _sp.run(
            ["gh", "pr", "edit", pr_ref, "--add-reviewer", reviewer],
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return None


def disarm_pr_auto_merge(
    pr_ref: str,
    *,
    timeout: int = _POST_COMMENT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> _sp.CompletedProcess[bytes] | None:
    """Disarm auto-merge on *pr_ref* via ``gh pr merge --disable-auto`` (#2771).

    Policy-free, like :func:`add_pr_reviewer`: returns the completed
    subprocess (inspect ``.returncode``) or None if it could not run or timed
    out. The caller verifies the outcome by reading the PR back with
    :func:`fetch_pr_head`; the read-back, not the exit code, decides.
    """
    try:
        return _sp.run(
            ["gh", "pr", "merge", pr_ref, "--disable-auto"],
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
