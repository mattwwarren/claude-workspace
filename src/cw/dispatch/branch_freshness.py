"""Ticket-branch staleness detection against ``origin/<default_branch>`` (#1823).

The pre-existing freshness gate (``dispatch/gating.py``) checks only the shared
*client checkout* at dispatch-tick preflight time — never the ticket's own
branch, and never at the REVIEW->approval boundary. This module answers the
narrower question that boundary needs: **is this ticket's branch behind
``origin/<default_branch>``, and do the intervening main commits touch at least
one file the branch itself touches?**

Deliberately *narrow* (the ticket's option B, not option A's "flag every lag"):
a branch that has merely fallen behind, where main's churn is disjoint from the
branch's own, is not stale in any way that invalidates a clean review — gating
it would spuriously park healthy tickets on every busy repo.

Two hard contracts:

* **Fail open, never raise.** Every unresolvable state (no worktree, no
  ``origin/<default>`` ref, detached HEAD, git missing, a non-zero git exit)
  resolves to ``False`` — "not stale". A false positive parks a healthy ticket
  and costs an operator; a false negative costs at most one stale review, which
  is the status quo this ticket improves on. Mirrors
  ``worktree.compute_branch_diff_scope``'s identical fail-to-None shape.
* **No network calls.** This runs inside the dispatch loop's
  ``dev_queue_lock()`` critical section. It reads the *already-fetched* local
  ``origin/<default_branch>`` ref and never fetches; detection latency is
  therefore bounded by the existing per-tick fetch cadence, by design.

Reuses ``cw.worktree``'s ``_run_git``/``_resolve_merge_base`` rather than
duplicating the subprocess wrapper and merge-base probe — ``worktree.py`` is
already over the module-size ceiling, so this ticket's new concern gets its own
module in the ``dispatch`` package instead of accreting there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.worktree import _resolve_merge_base, _run_git

if TYPE_CHECKING:
    from pathlib import Path


def _is_behind_default(worktree_path: Path, default_branch: str) -> bool:
    """True iff HEAD is strictly behind ``origin/<default_branch>``.

    Counts commits reachable from ``origin/<default_branch>`` but not from
    HEAD. Zero means the branch already contains everything main has (it may
    still be *ahead*, which is the normal case for a healthy ticket branch and
    is not staleness).

    Fail-open: a missing ``origin/<default_branch>`` ref makes ``rev-list``
    exit non-zero, a missing git binary raises ``OSError``, and a malformed
    count fails ``int()`` — all three resolve to ``False``.
    """
    try:
        result = _run_git(
            "rev-list",
            "--count",
            f"HEAD..origin/{default_branch}",
            cwd=worktree_path,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def _touched_files(worktree_path: Path, rev_range: str) -> set[str] | None:
    """Return the repo-relative paths *rev_range* touches, or ``None`` on failure.

    ``None`` means "unmeasurable" and is distinct from an empty set ("measured,
    touched nothing") — the caller fails open on the former and correctly
    reports no overlap on the latter.
    """
    try:
        result = _run_git(
            "diff",
            "--name-only",
            rev_range,
            cwd=worktree_path,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def has_overlapping_branch_staleness(
    worktree_path: Path | None, default_branch: str
) -> bool:
    """True iff *worktree_path*'s branch is stale in the file-overlapping sense.

    Three probes, each short-circuiting to ``False`` (fail open):

      1. The branch is behind ``origin/<default_branch>`` at all.
      2. A merge-base between the two resolves.
      3. ``merge_base..origin/<default>`` and ``merge_base..HEAD`` share at
         least one path.

    Callers must treat ``False`` as "no evidence of staleness", not as a
    positive assertion of freshness.
    """
    if worktree_path is None or not worktree_path.exists():
        return False
    if not _is_behind_default(worktree_path, default_branch):
        return False
    merge_base = _resolve_merge_base(worktree_path, default_branch)
    if merge_base is None:
        return False
    main_files = _touched_files(worktree_path, f"{merge_base}..origin/{default_branch}")
    if main_files is None:
        return False
    branch_files = _touched_files(worktree_path, f"{merge_base}..HEAD")
    if branch_files is None:
        return False
    return bool(main_files & branch_files)
