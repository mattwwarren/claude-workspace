"""Per-cycle start state for the codex fix loop (#2633).

A fix cycle is measured against its own start, not against ``HEAD`` or the
worktree at large. Before the fix invocation runs,
:func:`capture_cycle_baseline` checks that the worktree is clean — no
unfinished merge, cherry-pick or revert, no unmerged paths, nothing staged,
modified or untracked — and only then records the commit and tree the cycle
starts from. It never stages anything: staging a conflicted index would clear
its unmerged stages and write the conflict markers into the baseline tree as if
they were committed work.

A cw review artifact present at start (untracked, or tracked and modified) is
exempt from that check, recorded in the baseline, and unstaged every time the
cycle stages, so it never reaches a cycle commit (#2689).

**Invariant: a cycle starts only from a clean tree, so the baseline tree is
exactly ``HEAD``'s tree, and the cycle's commit carries only what the cycle
itself changed.** Nothing the cycle did not do can appear in
:func:`cycle_touched_paths`, :func:`cycle_diff`, the scope fence, the growth
budget or the commit. :func:`diff_since` generalizes :func:`cycle_diff` to any
base, which the loop-wide growth budget (#2648) uses with the loop's start
commit. A dirty start is reported as a :class:`DirtyStart`; the caller parks
the cycle uncommitted (``codex_fix_dirty_start``).

Pure measurement: this module imports nothing from the guard modules. It runs
inside the codex executor's review step, outside every lock.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_output, run_git

if TYPE_CHECKING:
    from pathlib import Path

# Unfinished-operation pseudo-refs whose presence refuses a cycle start.
_OPERATION_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
# `git rev-parse -q --verify` exit codes: 0 resolves, 1 does not; anything
# else is a git failure (fail closed: raise, the caller parks codex_error).
_REF_SET = 0
_REF_UNSET = 1
# Review artifacts the review step writes into the worktree
# (codex_background.REVIEW_VERDICT_COMMENT_RELATIVE_PATH,
# cw.review_findings.REVIEW_VERDICT_JSON_RELATIVE_PATH and
# codex_background.REVIEW_UNPARSEABLE_ARTIFACT_RELATIVE_PATH). Literals because
# codex_background imports this package; a lockstep test pins them. The review
# step rewrites them in place, so an untracked one or a tracked-modified one
# (` M`, `M `, `MM`) is cw-owned and ignored — a deleted, renamed or
# staged-new artifact is a real change and still makes the tree dirty.
_CW_REVIEW_ARTIFACTS = frozenset(
    {
        ".claude/review-verdict.md",
        ".claude/review-verdict.json",
        ".claude/review-verdict-unparseable.md",
    }
)
_UNTRACKED_PREFIX = "?? "
# Porcelain v1: a two-character status code and one space precede the path.
_PORCELAIN_PATH_OFFSET = 3
_STATUS_CODE_WIDTH = 2
# Tracked-modified porcelain codes: worktree-modified, staged, or both.
_MODIFIED_TRACKED_CODES = frozenset({" M", "M ", "MM"})


class CycleBaseline(NamedTuple):
    """The commit and tree a clean fix cycle starts from (tree == HEAD's tree).

    ``review_artifacts`` holds the cw review artifacts present at cycle start,
    untracked or tracked-modified; every cycle staging step unstages them.
    """

    head_sha: str
    tree_sha: str
    review_artifacts: frozenset[str] = frozenset()


class DirtyStart(NamedTuple):
    """What made the worktree unfit to start a fix cycle.

    ``markers`` are the unfinished-operation refs that resolve, ``unmerged``
    the sorted, de-duplicated ``git ls-files -u`` paths, and ``dirty`` the
    remaining ``git status --porcelain --untracked-files=all`` lines.
    """

    markers: tuple[str, ...]
    unmerged: tuple[str, ...]
    dirty: tuple[str, ...]


def _marker_is_set(worktree: Path, marker: str) -> bool:
    """Return whether *marker* resolves; raise on any git failure."""
    argv = ["rev-parse", "-q", "--verify", marker]
    result = run_git(argv, cwd=worktree, capture_output=True)
    if result.returncode == _REF_SET:
        return True
    if result.returncode == _REF_UNSET:
        return False
    raise subprocess.CalledProcessError(
        result.returncode, ["git", *argv], output=result.stdout, stderr=result.stderr
    )


def _unmerged_paths(worktree: Path) -> tuple[str, ...]:
    """Return the sorted unique paths ``git ls-files -u`` lists."""
    out = git_output(["ls-files", "-u"], cwd=worktree)
    return tuple(sorted({line.split("\t", 1)[1] for line in out.splitlines() if line}))


def _is_review_artifact(line: str) -> bool:
    """Whether a porcelain line is an untracked or modified cw review artifact."""
    return line[_PORCELAIN_PATH_OFFSET:] in _CW_REVIEW_ARTIFACTS and (
        line.startswith(_UNTRACKED_PREFIX)
        or line[:_STATUS_CODE_WIDTH] in _MODIFIED_TRACKED_CODES
    )


def unstage_review_artifacts(worktree: Path, artifacts: frozenset[str]) -> None:
    """Keep cw review artifacts present at cycle start out of cycle staging.

    For a tracked path ``git restore --staged`` resets the index entry to
    ``HEAD``; the worktree edit is left alone.
    """
    staged = git_output(
        ["diff", "--cached", "--name-only", "--no-renames"], cwd=worktree
    )
    staged_artifacts = sorted(path for path in staged.splitlines() if path in artifacts)
    if staged_artifacts:
        git_output(["restore", "--staged", "--", *staged_artifacts], cwd=worktree)


def capture_cycle_baseline(worktree: Path) -> CycleBaseline | DirtyStart:
    """Return the cycle's clean-start baseline, or what makes the tree dirty.

    Read-only and never stages. Probes, in order: the three
    unfinished-operation markers, ``git ls-files -u``, then
    ``git status --porcelain --untracked-files=all`` (minus untracked and
    tracked-modified cw review artifacts). Any finding returns a
    :class:`DirtyStart`. Any git failure propagates as ``CalledProcessError``.
    """
    markers = tuple(m for m in _OPERATION_MARKERS if _marker_is_set(worktree, m))
    unmerged = _unmerged_paths(worktree)
    status = git_output(
        ["status", "--porcelain", "--untracked-files=all"], cwd=worktree
    )
    status_lines = tuple(line for line in status.splitlines() if line)
    dirty = tuple(line for line in status_lines if not _is_review_artifact(line))
    artifacts = frozenset(
        line[_PORCELAIN_PATH_OFFSET:]
        for line in status_lines
        if _is_review_artifact(line)
    )
    if markers or unmerged or dirty:
        return DirtyStart(markers=markers, unmerged=unmerged, dirty=dirty)
    return CycleBaseline(
        head_sha=git_output(["rev-parse", "HEAD"], cwd=worktree).strip(),
        tree_sha=git_output(["rev-parse", "HEAD^{tree}"], cwd=worktree).strip(),
        review_artifacts=artifacts,
    )


def cycle_touched_paths(worktree: Path, baseline: CycleBaseline) -> set[str]:
    """Stage the cycle's changes and return every path differing from the baseline.

    This is where a cycle is first staged — after its fix invocation, never
    before it. ``--no-renames``: a rename reports both sides, matching
    :func:`cw.codex_fix_loop.fence._name_only`.
    """
    git_output(["add", "-A"], cwd=worktree)
    unstage_review_artifacts(worktree, baseline.review_artifacts)
    new_tree = git_output(["write-tree"], cwd=worktree).strip()
    out = git_output(
        ["diff-tree", "-r", "--name-only", "--no-renames", baseline.tree_sha, new_tree],
        cwd=worktree,
    )
    return {line for line in out.splitlines() if line}


def diff_since(worktree: Path, rev: str) -> str:
    """Return the staged zero-context diff against *rev* (a commit or tree).

    Against the fix loop's start commit (#2648) this is every earlier cycle's
    commit plus the current, still-staged cycle. Any git failure (an
    unresolvable *rev*) propagates as ``CalledProcessError``.
    """
    return git_output(["diff", "--cached", "-U0", "--no-renames", rev], cwd=worktree)


def cycle_diff(worktree: Path, baseline: CycleBaseline) -> str:
    """Return the staged zero-context diff of the cycle against its baseline tree."""
    return diff_since(worktree, baseline.tree_sha)
