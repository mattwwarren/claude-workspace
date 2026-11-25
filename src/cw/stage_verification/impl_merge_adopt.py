"""Adopting and concluding an unconcluded merge on a regress into IMPL (#3529).

A FINALIZE regress can leave the worktree mid-merge: the pre-push refresh
started ``git merge origin/<base>``, hit conflicts and stopped. Resolving that
merge is the point of the IMPL re-entry, so :func:`adopt_unconcluded_merge`
reads the merge as the same shape cw's own repair-row merge produces
(:class:`~cw.stage_verification.impl_feedback.MergeOutcome`) and the baseline
tolerates exactly it (``capture_cycle_baseline(..., merge_files=...)``).

cw, not the turn, concludes the merge: the codex turn runs ``workspace-write``
and cannot write the linked worktree's index, so ``git ls-files -u`` lists
every conflicted path before and after the turn, and cw's ``git add -A`` would
silently mark an untouched binary or modify/delete conflict resolved. Two
checks therefore run before anything is staged:

- :func:`unresolved_conflict_files`: a conflicted text file that still holds
  conflict markers;
- :func:`untouched_conflicts`: a conflicted path that is still unmerged AND
  byte-identical to its pre-turn content (:func:`conflict_fingerprints`).

:func:`merge_unconcluded_block` is the post-commit guard: the commit cw made
must have cleared ``MERGE_HEAD``. Every block is ``impl_failed`` with a
``merge_in_progress:`` details prefix; no new blocker reason exists.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw._git import git_output, run_git
from cw.branch_ahead import merge_in_progress
from cw.merge_probe import merge_tree
from cw.stage_verification.baseline import unmerged_paths
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_feedback import MergeOutcome
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

MERGE_IN_PROGRESS_PREFIX: Final = "merge_in_progress: "

_MARKER_LINE: Final = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)
_NOT_ANCESTOR_RC: Final = 1
_SHA_PREFIX_CHARS: Final = 12
_MAX_LISTED: Final = 20


@dataclass(frozen=True)
class AdoptedMerge:
    """An unconcluded merge adopted for resolution."""

    merge_head: str
    outcome: MergeOutcome


def _block(why: str) -> ImplBlock:
    return ImplBlock(IMPL_FAILED, f"{MERGE_IN_PROGRESS_PREFIX}{why}")


def _listing(paths: Iterable[str]) -> str:
    listed = sorted(paths)
    shown = ", ".join(listed[:_MAX_LISTED])
    extra = len(listed) - _MAX_LISTED
    return f"{shown} (and {extra} more)" if extra > 0 else shown


def adopt_unconcluded_merge(
    worktree: Path, *, base_branch: str
) -> AdoptedMerge | ImplBlock:
    """Read an arriving ``MERGE_HEAD`` as an adoptable default-branch merge.

    Adoptable only when ``MERGE_HEAD`` is an ancestor of ``origin/<base_branch>``:
    the merge cw adopts is a default-branch refresh, not an arbitrary merge.
    Any other state is a ``merge_in_progress:`` block. Read-only.
    """
    ref = f"origin/{base_branch}"
    try:
        merge_head = git_output(["rev-parse", "MERGE_HEAD"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        return _block(f"MERGE_HEAD could not be read: {redact_and_cap(str(exc))}")
    ancestor = run_git(
        ["merge-base", "--is-ancestor", merge_head, ref],
        cwd=worktree,
        capture_output=True,
    )
    if ancestor.returncode == _NOT_ANCESTOR_RC:
        return _block(
            f"MERGE_HEAD {merge_head[:_SHA_PREFIX_CHARS]} is not on {ref}, so cw "
            "does not adopt it; conclude or abort the merge, then requeue"
        )
    if ancestor.returncode != 0:
        tail = redact_and_cap((ancestor.stderr or "").strip())
        return _block(f"could not check MERGE_HEAD against {ref}: {tail}")
    names = git_output(
        ["diff", "--name-only", "--no-renames", "HEAD...MERGE_HEAD"], cwd=worktree
    )
    tree = merge_tree(worktree, "HEAD", "MERGE_HEAD")
    if tree.tree is None:
        return _block(
            "cannot measure the unconcluded merge: "
            + redact_and_cap(tree.detail or tree.status)
        )
    outcome = MergeOutcome(
        merge_files=frozenset(line for line in names.splitlines() if line),
        growth_tree=tree.tree,
        conflicted=frozenset(unmerged_paths(worktree)),
    )
    return AdoptedMerge(merge_head, outcome)


def unresolved_conflict_files(
    worktree: Path, conflicted: Iterable[str]
) -> tuple[str, ...]:
    """The conflicted paths whose text still holds ``<<<<<<<`` or ``>>>>>>>`` lines.

    Only the conflicted paths are read: a merge-side file that merely contains
    the literals is not a conflict. An unreadable, absent or binary path is
    skipped (:func:`untouched_conflicts` covers it).
    """
    found: list[str] = []
    for path in sorted(conflicted):
        try:
            text = (worktree / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _MARKER_LINE.search(text):
            found.append(path)
    return tuple(found)


def _fingerprint(worktree: Path, path: str) -> str:
    """``git hash-object`` of *path*'s worktree content; ``""`` when absent."""
    if not (worktree / path).is_file():
        return ""
    return git_output(["hash-object", "--", path], cwd=worktree).strip()


def conflict_fingerprints(worktree: Path, conflicted: Iterable[str]) -> dict[str, str]:
    """Each conflicted path's current content hash (``""`` when the file is absent)."""
    return {path: _fingerprint(worktree, path) for path in sorted(conflicted)}


def untouched_conflicts(worktree: Path, before: dict[str, str]) -> tuple[str, ...]:
    """The *before* paths still unmerged (``git ls-files -u``), content unchanged.

    The turn cannot write the index, so a path it resolved leaves ``ls-files
    -u`` only after cw stages; the content comparison is what tells a resolved
    path from a never-touched one. Call it before anything is staged.
    """
    still = set(unmerged_paths(worktree))
    return tuple(
        path
        for path, fingerprint in sorted(before.items())
        if path in still and _fingerprint(worktree, path) == fingerprint
    )


def merge_unresolved_block(
    worktree: Path, conflicted: Iterable[str], before: dict[str, str]
) -> ImplBlock | None:
    """A ``merge_in_progress:`` block when the turn left a conflict unresolved."""
    markers = unresolved_conflict_files(worktree, conflicted)
    untouched = untouched_conflicts(worktree, before)
    if not markers and not untouched:
        return None
    parts: list[str] = []
    if markers:
        parts.append(f"conflict markers remain in {_listing(markers)}")
    if untouched:
        parts.append(
            f"{_listing(untouched)} is still unmerged and was not changed by the "
            "turn (change the file to resolve it, or conclude the merge by hand)"
        )
    return _block(
        "; ".join(parts) + ". Nothing was committed or pushed, and the merge "
        "state was dropped with the refused turn."
    )


def merge_unconcluded_block(worktree: Path) -> ImplBlock | None:
    """The post-commit guard: ``MERGE_HEAD`` must be gone after cw's commit."""
    pending = merge_in_progress(worktree)
    if pending is False:
        return None
    why = (
        "MERGE_HEAD is still set after cw's commit"
        if pending
        else "cw could not verify that the merge concluded"
    )
    return _block(why + "; the commit stays local and nothing was pushed")
