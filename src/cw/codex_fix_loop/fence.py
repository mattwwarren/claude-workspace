"""Fix-cycle scope fence and revert guard for the codex fix loop (#2485, #2492).

Runs after a fix invocation edits the worktree and before
:func:`~cw.codex_fix_loop.commit._commit_fix_cycle` commits it. Two checks,
both measured on the branch's *net* diff against its merge-base with the
default branch, with the cycle's pending changes staged:

- **Scope fence (#2485).** Every path whose content the cycle leaves differing
  from the base must sit in the fence: the plan's ``## Files Modified``
  manifest plus the cycle-0 reviewed diff's file set. An operator
  ``--scope-drift`` grant needs no separate input — the IMPL session that
  honored it put those files into the cycle-0 diff. Measuring the net diff
  rather than the raw touched set is what makes a *revert* of out-of-scope
  code pass: deleting a file an earlier cycle added, or restoring one to its
  base content, leaves it out of the net diff entirely.
- **Revert guard (#2492).** A fix that restores more than half of the
  branch's changed files to their base content — not counting files an open
  finding names — or empties the branch diff outright, is undoing the
  feature rather than patching it.

The fence is skipped when the plan has no parseable manifest, matching
:mod:`cw.plan_files`'s "empty manifest means no file fence" convention; the
revert guard always runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.codex_review import CODEX_FIX_REVERTED_BRANCH, CODEX_FIX_SCOPE_DRIFT
from cw.plan_files import parse_plan_files_modified

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# Shared tail for every fence breach's recovery hint: the rejected changes are
# left staged (never committed or pushed), so the operator can inspect them.
_LEFT_STAGED_HINT = (
    "The rejected fix-cycle changes are left staged and uncommitted in the "
    "worktree (inspect with `git diff --cached`); discard them with "
    "`git reset --hard HEAD` before requeueing."
)


@dataclass(frozen=True)
class FenceBreach:
    """A fix cycle the fence rejected: the park reason, paths, and operator text."""

    reason: str
    paths: tuple[str, ...]
    details: str
    recovery_hint: str


def fix_scope_allowlist(
    plan_text: str | None, cycle0_files: Iterable[str]
) -> frozenset[str] | None:
    """Return the fix loop's file fence, or ``None`` when no plan manifest exists.

    The fence is the plan's ``## Files Modified`` manifest unioned with the
    cycle-0 reviewed diff's file set. ``None`` (no fence) when the plan is
    absent or its manifest parses empty — a plan predating the manifest
    convention must not park every fix that touches any file.
    """
    if plan_text is None:
        return None
    planned = parse_plan_files_modified(plan_text)
    if not planned:
        return None
    return frozenset(planned) | frozenset(cycle0_files)


def _name_only(worktree: Path, args: list[str]) -> set[str]:
    """Return the path set ``git diff --name-only --no-renames <args>`` prints."""
    out = git_output(["diff", "--name-only", "--no-renames", *args], cwd=worktree)
    return {line for line in out.splitlines() if line}


def _revert_breach(
    before: set[str], after: set[str], finding_files: frozenset[str], cycle: int
) -> FenceBreach | None:
    """Return a revert-guard breach for this cycle, or ``None`` (#2492)."""
    reverted = before - after
    unnamed = reverted - finding_files
    emptied = bool(before) and not after
    if not emptied and len(unnamed) * 2 <= len(before):
        return None
    paths = tuple(sorted(reverted))
    headline = (
        f"codex fix cycle {cycle} would empty the branch diff: every file the "
        "branch changed would match the default branch again."
        if emptied
        else f"codex fix cycle {cycle} would restore {len(unnamed)} of the "
        f"branch's {len(before)} changed file(s) to their default-branch "
        "content, none of them named by an open finding."
    )
    details = "\n".join(
        [
            headline,
            "A fix cycle that undoes earlier commits is reverting the feature, "
            "not patching a finding. Restored file(s):",
            *(f"- {path}" for path in paths),
        ]
    )
    hint = (
        "Compare the branch HEAD (the last good commit before this fix cycle) "
        "with the staged changes to see what the fix tried to undo, then "
        "requeue REVIEW, or settle the finding (`cw review settle`) if it "
        f"cannot be fixed without reverting the feature. {_LEFT_STAGED_HINT}"
    )
    return FenceBreach(CODEX_FIX_REVERTED_BRANCH, paths, details, hint)


def _scope_breach(
    after: set[str], touched: set[str], allowed: frozenset[str], cycle: int
) -> FenceBreach | None:
    """Return a scope-fence breach for this cycle, or ``None`` (#2485).

    Only paths this cycle itself touched are blamed: a path already off-fence
    at HEAD (which the fence, active from cycle 1, should make impossible) is
    not this cycle's doing.
    """
    drifted = tuple(sorted((after & touched) - allowed))
    if not drifted:
        return None
    details = "\n".join(
        [
            f"codex fix cycle {cycle} changed path(s) outside the plan's "
            "Files Modified manifest and the cycle-0 reviewed diff:",
            *(f"- {path}" for path in drifted),
        ]
    )
    hint = (
        "If the out-of-scope change is wanted, add the path(s) to the plan's "
        "Files Modified section and requeue REVIEW; otherwise settle the "
        "finding that asked for it (`cw review settle`) or file it as a "
        f"follow-up ticket. {_LEFT_STAGED_HINT}"
    )
    return FenceBreach(CODEX_FIX_SCOPE_DRIFT, drifted, details, hint)


def check_fix_fence(
    worktree: Path,
    *,
    default_branch: str,
    allowed_files: frozenset[str] | None,
    finding_files: frozenset[str],
    cycle: int,
) -> FenceBreach | None:
    """Stage the cycle's changes and return the first fence breach, or ``None``.

    Stages with ``git add -A`` so new files count toward the net diff; the
    commit step stages the same way, so a passing cycle loses nothing. The
    revert guard is checked first: a cycle that undoes the branch is the more
    fundamental failure, and its restored files would otherwise read as
    in-scope.
    """
    git_output(["add", "-A"], cwd=worktree)
    touched = _name_only(worktree, ["--cached", "HEAD"])
    if not touched:
        return None
    base = git_output(["merge-base", default_branch, "HEAD"], cwd=worktree).strip()
    before = _name_only(worktree, [base, "HEAD"])
    after = _name_only(worktree, ["--cached", base])
    breach = _revert_breach(before, after, finding_files, cycle)
    if breach is None and allowed_files is not None:
        breach = _scope_breach(after, touched, allowed_files, cycle)
    return breach
