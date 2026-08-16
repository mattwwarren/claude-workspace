"""Commits-ahead measurement for a ticket branch (#1870).

Answers one question: **how many commits does this worktree's HEAD carry that
``origin/<default_branch>`` does not?** Zero means the branch exists but holds
nothing — there is no diff to review, nothing to ship, and no scope decision an
operator could meaningfully approve.

Two hard contracts, both inherited from ``dispatch/branch_freshness``:

* **Fail open, never raise.** Every unresolvable state (no worktree, no
  ``origin/<default>`` ref, git missing, a non-zero git exit, an unparseable
  count) resolves to ``None`` — "unmeasurable" — which callers must treat as
  "no evidence", never as "zero". The three-valued return is the whole point:
  conflating ``None`` with ``0`` would park every ticket whose git state cannot
  be read, and conflating ``0`` with ``None`` would restore the silent
  empty-diff advance this module exists to stop.
* **No network calls.** The dispatch-side caller runs inside the dispatch
  loop's ``dev_queue_lock()`` critical section. This reads the *already-fetched*
  local ``origin/<default_branch>`` ref and never fetches.

**Why a top-level module rather than one under ``cw.dispatch``.**
``branch_freshness`` is the near-exact structural sibling and would be the
natural home, but it lives inside the ``dispatch`` package, and
``dispatch/routing.py`` imports ``cw.codex_review`` at module top. Since
``codex_review._verdict`` needs this same measurement, putting it under
``cw.dispatch.*`` would create a genuine ``codex_review`` -> ``dispatch`` ->
``codex_review`` import cycle. ``cw.worktree`` — whose ``_run_git`` this reuses,
the same cross-module reuse ``branch_freshness`` already established — is
likewise unavailable as a home: it is already over CLAUDE.md's ~1000-line
module ceiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.worktree import _run_git

if TYPE_CHECKING:
    from pathlib import Path


def commits_ahead_of_default(
    worktree_path: Path | None, default_branch: str
) -> int | None:
    """Return HEAD's commit count ahead of ``origin/<default_branch>``.

    ``0`` means measured-and-empty (the #1870 gate condition), a positive int
    means measured-with-work, and ``None`` means the measurement could not be
    taken at all. Callers MUST distinguish the last from the first.

    Mirrors ``branch_freshness._is_behind_default``'s probe shape with the
    range reversed and the raw count returned rather than a bool, so both the
    dispatch gate and the review-synthesis path can tell the three cases apart.
    """
    if worktree_path is None or not worktree_path.exists():
        return None
    try:
        result = _run_git(
            "rev-list",
            "--count",
            f"origin/{default_branch}..HEAD",
            cwd=worktree_path,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None
