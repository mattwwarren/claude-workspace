"""Unsaved-work (dirty-tree / unpushed-commit) detection for :mod:`cw.worktree`.

:func:`unsaved_work_reason` and its boolean view
:func:`worktree_has_unsaved_work`, with the remote-ref ladders they measure
unpushed commits against.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.exceptions import WorktreeError
from cw.worktree._git import (
    _GIT_PORCELAIN_PATH_OFFSET,
    _checked_out_branch,
    _run_git,
)
from cw.worktree._paths import worktree_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig

_log = logging.getLogger(__name__)


# cw-managed per-session scratch files live under this prefix. They are written
# fresh each spawn and must not count as real uncommitted work.
# Mirrored in worktree_gc._CW_SCRATCH_PREFIX (duplicated per D5 to avoid
# importing private names cross-module).
_CW_SCRATCH_PREFIX = ".claude/"


def _commits_ahead(base: str, wt_path: Path) -> int | None:
    """Return the number of commits in ``<base>..HEAD``, or None if *base* is
    not resolvable (``git log`` exits non-zero)."""
    result = _run_git("log", f"{base}..HEAD", "--oneline", cwd=wt_path, check=False)
    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _own_remote_ref(branch: str, wt_path: Path) -> str | None:
    """Return ``origin/<checked-out branch>`` when that ref exists, else None.

    The checked-out branch is preferred over the caller-supplied *branch*
    (#2050/#2053: the two can differ); *branch* is the fallback when the
    checkout cannot be resolved. ``rev-parse --verify`` answers "is this
    branch pushed" exactly, independent of tracking configuration (#2114).
    """
    current = _checked_out_branch(wt_path) or branch
    ref = f"origin/{current}"
    verify = _run_git("rev-parse", "--verify", "--quiet", ref, cwd=wt_path, check=False)
    return ref if verify.returncode == 0 else None


def _upstream_ref(wt_path: Path) -> str | None:
    """Return the checked-out branch's ``@{u}`` tracking ref, or None."""
    upstream = _run_git(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        cwd=wt_path,
        check=False,
    )
    if upstream.returncode != 0:
        return None
    return upstream.stdout.strip() or None


def _unpushed_commits_detail(
    client: ClientConfig, branch: str, wt_path: Path
) -> str | None:
    """Describe *branch*'s unpushed commits, or return None when it is pushed.

    Base-ref ladder, first resolvable level wins:

    0. ``origin/<checked-out branch>`` when that ref exists — the exact
       answer to "is this branch pushed". This level is what #2114 added:
       the ``@{u}`` level below trusts whatever tracking ref is configured,
       and when that is ``origin/<default_branch>`` every commit the feature
       branch contains reads as unpushed, so a clean, fully-pushed worktree
       parked ``dirty_worktree`` forever.
    1. The worktree's own ``@{u}`` upstream (a branch pushed under another
       name, #2050/#2053).
    2. ``origin/<default_branch>`` — no own remote ref, no upstream.
    3. Local ``<default_branch>`` — offline / bare-clone fallback.

    Levels 1-3 cannot distinguish "pushed under a name we cannot see" from
    "never pushed", so their message says which base was measured against;
    the caller surfaces it in the park breadcrumb. Returns a description
    conservatively on subprocess failure or when no base ref resolves.
    """
    try:
        own = _own_remote_ref(branch, wt_path)
        if own is not None:
            ahead = _commits_ahead(own, wt_path)
            if ahead is not None:
                return None if ahead == 0 else f"{ahead} commit(s) not on {own}"
        candidates = (
            _upstream_ref(wt_path),
            f"origin/{client.default_branch}",
            client.default_branch,
        )
        for base in (b for b in candidates if b is not None):
            ahead = _commits_ahead(base, wt_path)
            if ahead is None:
                continue
            if ahead == 0:
                return None
            return (
                f"{ahead} commit(s) ahead of {base} and no origin/<branch> ref"
                " for the checked-out branch (cannot prove they are pushed)"
            )
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: log check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work.
        return f"unpushed-commit check failed: {exc}"
    # All refs unresolvable — conservative fail-safe.
    return "no base ref resolvable (offline or bare clone)"


def _uncommitted_changes_detail(
    client: ClientConfig, branch: str, wt_path: Path
) -> str | None:
    """Describe uncommitted changes in *wt_path*, or return None when clean.

    Filters out cw's own artifacts (``.claude/``) — these are written fresh
    each session and would otherwise trip the dirty check on every retry.
    Porcelain format: "XY path" (2-char status + space + path). Rename
    entries ("R  old -> new") pass through unchanged; cw artifacts never
    appear as renames so they will still be caught by path check.
    """
    try:
        status = _run_git("status", "--porcelain", cwd=wt_path, check=False)
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: status check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work so we don't silently destroy.
        return f"status check failed: {exc}"
    lines = [
        line
        for line in status.stdout.splitlines()
        if not (
            len(line) > _GIT_PORCELAIN_PATH_OFFSET
            and line[_GIT_PORCELAIN_PATH_OFFSET:].startswith(_CW_SCRATCH_PREFIX)
        )
    ]
    if not lines:
        return None
    return f"{len(lines)} uncommitted path(s)"


def unsaved_work_reason(
    client: ClientConfig, branch: str, *, wt_path: Path | None = None
) -> str | None:
    """Return why the worktree for *branch* has unsaved work, or None if clean.

    "Unsaved" means either:
    - uncommitted changes (``git status --porcelain`` is non-empty), OR
    - unpushed commits (``git log <base>..HEAD`` is non-empty, see
      :func:`_unpushed_commits_detail` for the base-ref ladder).

    The returned string names which predicate fired, the base ref it was
    measured against, and the count — a park that says only
    ``dirty_worktree`` on a visibly clean tree cost real operator time
    before the predicate was read (#2114). Returns None when the worktree
    path does not exist (nothing to lose).

    *wt_path* defaults to the branch's canonical ``worktree_path_for(client,
    branch)`` location. Passing it explicitly lets a caller check a *foreign*
    (non-canonical) worktree's dirty state instead — e.g. a worktree
    collision's holder path, which cw did not create and does not track
    (#2034).

    Never raises — every git error is swallowed and logged at WARNING level
    so that a git failure cannot block a cleanup sweep; it is reported as a
    reason instead (fail-safe toward "has unsaved work").
    """
    if wt_path is None:
        wt_path = worktree_path_for(client, branch)
    if not wt_path.exists():
        return None
    uncommitted = _uncommitted_changes_detail(client, branch, wt_path)
    if uncommitted is not None:
        return uncommitted
    return _unpushed_commits_detail(client, branch, wt_path)


def worktree_has_unsaved_work(
    client: ClientConfig, branch: str, *, wt_path: Path | None = None
) -> bool:
    """Return True if the worktree for *branch* has unsaved work.

    Boolean view of :func:`unsaved_work_reason`; see it for the contract.
    """
    return unsaved_work_reason(client, branch, wt_path=wt_path) is not None
