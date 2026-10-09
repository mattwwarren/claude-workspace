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

Since #2633 the fence measures the cycle from its own start: the caller
measures the cycle's touched paths once against the clean-start baseline
(:mod:`cw.codex_fix_loop.baseline`) and hands them in. This module also builds
the breach records for the other guards that park a cycle uncommitted: the
dirty-start refusal, the sensitive-path scope violation and the staged-set
mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.codex_review import (
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_REVERTED_BRANCH,
    CODEX_FIX_SCOPE_DRIFT,
    CODEX_FIX_SCOPE_VIOLATION,
)
from cw.plan_files import parse_plan_files_modified

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from cw.codex_fix_loop.baseline import DirtyStart
    from cw.codex_review import _SensitiveHit

# Shared tail for every guard park's recovery hint: the rejected changes are
# left staged (never committed or pushed), so the operator can inspect them.
# The hard-reset advice is only correct because a cycle starts from a clean
# tree (#2633, `baseline.capture_cycle_baseline`), and the hint says so.
LEFT_STAGED_HINT = (
    "The rejected fix-cycle changes are left staged and uncommitted in the "
    "worktree (inspect with `git diff --cached`). This cycle began from a clean "
    "tree (checked at cycle start), so everything staged is this cycle's own "
    "work and `git reset --hard HEAD` discards only that; run it before "
    "requeueing."
)
# Listing cap for the dirty-start and staged-set park details.
_MAX_LISTED = 20
_PORCELAIN_PATH_OFFSET = 3
_MARKER_LINES = {
    "MERGE_HEAD": "MERGE_HEAD is set (an uncommitted merge)",
    "CHERRY_PICK_HEAD": "CHERRY_PICK_HEAD is set (an unfinished cherry-pick)",
    "REVERT_HEAD": "REVERT_HEAD is set (an unfinished revert)",
}
_MARKER_HINTS = {
    "MERGE_HEAD": (
        "Finish the merge (resolve conflicts, `git add` them, then `git commit`) "
        "or abandon it with `git merge --abort`."
    ),
    "CHERRY_PICK_HEAD": (
        "Finish the cherry-pick or abandon it with `git cherry-pick --abort`."
    ),
    "REVERT_HEAD": "Finish the revert or abandon it with `git revert --abort`.",
}
_NO_MARKER_HINT = (
    "Commit the changes you want to keep, or set them aside (for "
    "example on a scratch branch) after checking `git diff HEAD`; cw does not "
    "know which of them are wanted, and it names no discard command because it "
    "cannot say what one would lose."
)


@dataclass(frozen=True)
class FenceBreach:
    """A fix cycle a guard rejected: the park reason, paths, and operator text."""

    reason: str
    paths: tuple[str, ...]
    details: str
    recovery_hint: str


class StagedSetMismatchError(Exception):
    """The staged set differs from the paths the cycle was measured to touch."""

    def __init__(self, measured: frozenset[str], staged: frozenset[str]) -> None:
        self.measured = measured
        self.staged = staged
        super().__init__(
            f"staged {sorted(staged)} != measured {sorted(measured)}",
        )


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
        f"cannot be fixed without reverting the feature. {LEFT_STAGED_HINT}"
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
        f"follow-up ticket. {LEFT_STAGED_HINT}"
    )
    return FenceBreach(CODEX_FIX_SCOPE_DRIFT, drifted, details, hint)


def check_fix_fence(
    worktree: Path,
    *,
    default_branch: str,
    allowed_files: frozenset[str] | None,
    finding_files: frozenset[str],
    cycle: int,
    touched: set[str],
) -> FenceBreach | None:
    """Return the first fence breach for the cycle's staged changes, or ``None``.

    *touched* is the set :func:`~cw.codex_fix_loop.baseline.cycle_touched_paths`
    measured against the cycle's clean-start baseline; that call also staged
    the cycle's changes (``git add -A``) so new files count toward the net
    diff. The revert guard is checked first: a cycle that undoes the branch is
    the more fundamental failure, and its restored files would otherwise read
    as in-scope.
    """
    if not touched:
        return None
    base = git_output(["merge-base", default_branch, "HEAD"], cwd=worktree).strip()
    before = _name_only(worktree, [base, "HEAD"])
    after = _name_only(worktree, ["--cached", base])
    breach = _revert_breach(before, after, finding_files, cycle)
    if breach is None and allowed_files is not None:
        breach = _scope_breach(after, touched, allowed_files, cycle)
    return breach


def _capped(entries: Sequence[str]) -> list[str]:
    """``- <entry>`` lines, at most :data:`_MAX_LISTED`, then a remainder count."""
    lines = [f"- {entry}" for entry in entries[:_MAX_LISTED]]
    if len(entries) > _MAX_LISTED:
        lines.append(f"- ... and {len(entries) - _MAX_LISTED} more")
    return lines


def _porcelain_paths(line: str) -> list[str]:
    """Both sides of a porcelain rename line, else the one path it names."""
    return line[_PORCELAIN_PATH_OFFSET:].split(" -> ")


def cycle_start_breach(start: DirtyStart, cycle: int) -> FenceBreach:
    """Return the park for a cycle refused because the tree was dirty (#2633).

    The hint never advises ``git reset --hard``: the state predates the cycle,
    so cw cannot say what a hard reset would lose. It names the state to
    inspect and each unfinished operation's own abort command.
    """
    sections = [
        f"codex fix cycle {cycle} was not started: the worktree was not clean at "
        "cycle start, so the cycle's own edits could not be told apart from work "
        "already there. No fix invocation ran, and cw staged, committed and "
        "pushed nothing."
    ]
    if start.markers:
        sections.append(
            "\n".join(
                [
                    "Unfinished operation(s):",
                    *_capped([_MARKER_LINES[m] for m in start.markers]),
                ]
            )
        )
    if start.unmerged:
        sections.append(
            "\n".join(["Unresolved (unmerged) path(s):", *_capped(start.unmerged)])
        )
    if start.dirty:
        sections.append(
            "\n".join(
                ["Uncommitted changes (git status --porcelain):", *_capped(start.dirty)]
            )
        )
    operations = " ".join(_MARKER_HINTS[m] for m in start.markers) or _NO_MARKER_HINT
    hint = (
        "Inspect the state first: `git status`, `git diff --cached` and "
        f"`git ls-files -u` show what is there. {operations} Then requeue REVIEW."
    )
    paths = {*start.unmerged, *(p for d in start.dirty for p in _porcelain_paths(d))}
    return FenceBreach(
        CODEX_FIX_DIRTY_START, tuple(sorted(paths)), "\n\n".join(sections), hint
    )


def _staged_set_hint(
    measured: frozenset[str], staged: frozenset[str], start_head: str
) -> str:
    """The staged-set park's hint for the case the two sets reveal."""
    if staged - measured:
        return (
            "Some staged paths were not measured as this cycle's work, so cw "
            "cannot say whether discarding them is safe. Compare "
            "`git diff --cached --name-only` with the paths above, then unstage "
            "the unmeasured paths (`git restore --staged <path>`) or "
            "commit them deliberately, and requeue REVIEW."
        )
    if not staged:
        return (
            "The fix invocation committed its change itself, so nothing is staged "
            "and a hard reset to HEAD would not undo that local commit. Inspect it "
            f"with `git log {start_head}..HEAD` and `git show`; if it is unwanted, "
            f"move the branch back to `{start_head}` after checking "
            "nothing else sits on top, then requeue REVIEW."
        )
    return (
        "Only part of what this cycle was measured to touch is staged, so a "
        "commit would be partial and cw does not know where the rest went. "
        "Compare `git diff --cached --name-only` with the paths above and run "
        f"`git log {start_head}..HEAD` to see whether the fix invocation "
        "committed the remainder itself; resolve it (commit, unstage or "
        "move the branch) and requeue REVIEW."
    )


def staged_set_breach(
    measured: frozenset[str], staged: frozenset[str], cycle: int, *, start_head: str
) -> FenceBreach:
    """Return the park for a cycle whose staged set is not its measured set.

    Parks under ``codex_fix_scope_drift``: the commit would carry (or miss) a
    path the cycle was not measured to touch. Unreachable under the
    clean-start invariant except through a race or a fix invocation that
    committed on its own, so its hint does NOT append :data:`LEFT_STAGED_HINT`
    — that hint's premise is exactly what failed here.
    """
    sections = [
        f"codex fix cycle {cycle} was not committed: the staged set does not "
        "equal the set of paths this cycle was measured to touch. A fix cycle "
        "commits and pushes only paths it measured."
    ]
    extra = sorted(staged - measured)
    missing = sorted(measured - staged)
    if extra:
        sections.append("\n".join(["Staged but not measured:", *_capped(extra)]))
    if missing:
        sections.append("\n".join(["Measured but not staged:", *_capped(missing)]))
    return FenceBreach(
        CODEX_FIX_SCOPE_DRIFT,
        tuple(sorted(measured ^ staged)),
        "\n\n".join(sections),
        _staged_set_hint(measured, staged, start_head),
    )


def scope_violation_breach(violations: list[_SensitiveHit], cycle: int) -> FenceBreach:
    """Return the park for a cycle touching an out-of-scope sensitive path.

    The gate is AND-only: the caller passes hits already computed over the
    out-of-scope subset, so both conditions (out of the cycle-0 reviewed
    diff's scope, and a sensitive-registry match) hold for every listed path.
    Measuring the cycle stages its changes (#2633), so the hint says where
    they are.
    """
    details = "\n".join(
        [
            f"codex fix cycle {cycle} touched path(s) that are both out of the "
            "cycle-0 reviewed diff's scope AND match the sensitive-files "
            "registry:",
            *(f"- {hit.path} ({hit.category}): {hit.reason}" for hit in violations),
        ]
    )
    hint = (
        "If the sensitive change is wanted, add the path(s) to the plan and get "
        "it reviewed, then requeue REVIEW; otherwise settle the finding that "
        f"asked for it (`cw review settle`). {LEFT_STAGED_HINT}"
    )
    return FenceBreach(
        CODEX_FIX_SCOPE_VIOLATION,
        tuple(hit.path for hit in violations),
        details,
        hint,
    )
