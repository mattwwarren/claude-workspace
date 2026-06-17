"""Worktree GC: remove worktrees for squash-merged or closed branches via PR state."""

from __future__ import annotations

import enum
import json
import logging
import os
import subprocess as _sp
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# git worktree list --porcelain field prefixes
_PORCELAIN_WORKTREE = "worktree "
_PORCELAIN_BRANCH = "branch refs/heads/"
_PORCELAIN_LOCKED = "locked"
_PORCELAIN_DETACHED = "detached"
_PORCELAIN_BARE = "bare"
_PORCELAIN_HEAD = "HEAD "

# gh CLI subcommand args
_GH_PR_LIST_STATE_ALL = "all"
_GH_PR_LIST_LIMIT = "1"

# gh PR state values returned in JSON output
_GH_PR_STATE_MERGED = "MERGED"
_GH_PR_STATE_CLOSED = "CLOSED"
_GH_PR_STATE_OPEN = "OPEN"

# Why -D not -d: squash-merged branches are never ancestors of main, so -d
# (safe delete) always refuses them. We verify PR state and check for unsaved
# work before removing, so force-delete here is safe.
_GIT_BRANCH_DELETE_FLAG = "-D"


def _git_clean_env() -> dict[str, str]:
    """Return os.environ with GIT_* vars stripped.

    GIT_* vars (e.g. GIT_DIR, GIT_WORK_TREE) can misdirect git commands
    when the calling process itself runs inside a git repo context.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class GcVerdict(enum.Enum):
    """Classification outcome for a single worktree."""

    REMOVE_MERGED = "REMOVE_MERGED"
    REMOVE_CLOSED = "REMOVE_CLOSED"
    KEEP_OPEN_PR = "KEEP_OPEN_PR"
    KEEP_NO_PR = "KEEP_NO_PR"
    KEEP_CLOSED_PR = "KEEP_CLOSED_PR"
    SKIP_LOCKED = "SKIP_LOCKED"
    SKIP_GH_UNAVAILABLE = "SKIP_GH_UNAVAILABLE"
    SKIP_DETACHED = "SKIP_DETACHED"
    SKIP_BARE = "SKIP_BARE"
    SKIP_DIRTY = "SKIP_DIRTY"


# Canonical verdict partitions — single source of truth for all three report properties
# and any formatter. Adding a new GcVerdict requires updating exactly one set here.
GC_REMOVE_VERDICTS: frozenset[GcVerdict] = frozenset(
    {GcVerdict.REMOVE_MERGED, GcVerdict.REMOVE_CLOSED}
)
GC_KEEP_VERDICTS: frozenset[GcVerdict] = frozenset(
    {GcVerdict.KEEP_OPEN_PR, GcVerdict.KEEP_NO_PR, GcVerdict.KEEP_CLOSED_PR}
)
GC_SKIP_VERDICTS: frozenset[GcVerdict] = frozenset(
    {
        GcVerdict.SKIP_LOCKED,
        GcVerdict.SKIP_GH_UNAVAILABLE,
        GcVerdict.SKIP_DETACHED,
        GcVerdict.SKIP_BARE,
        GcVerdict.SKIP_DIRTY,
    }
)


@dataclass(frozen=True)
class WorktreeEntry:
    """A single git worktree parsed from porcelain output."""

    path: Path
    branch: str | None  # None means detached HEAD
    locked: bool
    is_bare: bool = field(default=False)


@dataclass(frozen=True)
class WorktreeGcResult:
    """Classification result for a single WorktreeEntry."""

    entry: WorktreeEntry
    verdict: GcVerdict
    pr_number: int | None


@dataclass
class WorktreeGcReport:
    """Aggregated output from :func:`run_worktree_gc`."""

    results: list[WorktreeGcResult]
    removal_failures: int = 0  # REMOVE_* worktrees where git worktree remove failed

    @property
    def to_remove(self) -> list[WorktreeGcResult]:
        """Results that should be (or were) removed."""
        return [r for r in self.results if r.verdict in GC_REMOVE_VERDICTS]

    @property
    def kept(self) -> list[WorktreeGcResult]:
        """Results kept (open/no/closed PR without --include-closed)."""
        return [r for r in self.results if r.verdict in GC_KEEP_VERDICTS]

    @property
    def skipped(self) -> list[WorktreeGcResult]:
        """Results skipped (locked, dirty, detached, bare, or gh unavailable)."""
        return [r for r in self.results if r.verdict in GC_SKIP_VERDICTS]


def _parse_worktree_blocks(stdout: str) -> list[dict[str, str]]:
    """Parse porcelain worktree output into a list of field dicts.

    Each block is separated by a blank line. Fields are key-value where
    ``worktree``, ``HEAD``, ``branch`` have a value; ``locked``, ``detached``,
    and ``bare`` are bare flags (value may be a reason string).
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = {}
            continue
        if stripped.startswith(_PORCELAIN_WORKTREE):
            current["worktree"] = stripped[len(_PORCELAIN_WORKTREE) :]
        elif stripped.startswith(_PORCELAIN_BRANCH):
            current["branch"] = stripped[len(_PORCELAIN_BRANCH) :]
        elif stripped == _PORCELAIN_DETACHED:
            current["detached"] = "1"
        elif stripped.startswith(_PORCELAIN_LOCKED):
            current["locked"] = "1"
        elif stripped == _PORCELAIN_BARE:
            current["bare"] = "1"
        elif stripped.startswith(_PORCELAIN_HEAD):
            current["HEAD"] = stripped[len(_PORCELAIN_HEAD) :]
    if current:
        blocks.append(current)
    return blocks


def list_repo_worktrees(git_cwd: Path) -> list[WorktreeEntry]:
    """Return all non-main worktrees registered in the repo at *git_cwd*.

    The main checkout (first entry from ``git worktree list --porcelain``,
    which has the same path as *git_cwd*) is excluded from the result.
    Returns an empty list on any subprocess failure.
    """
    clean_env = _git_clean_env()
    try:
        result = _sp.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(git_cwd),
            env=clean_env,
        )
    except (OSError, FileNotFoundError) as exc:
        _log.warning("list_repo_worktrees: git failed for %s: %s", git_cwd, exc)
        return []

    if result.returncode != 0:
        _log.warning(
            "list_repo_worktrees: git worktree list failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return []

    blocks = _parse_worktree_blocks(result.stdout)
    entries: list[WorktreeEntry] = []
    for block in blocks:
        wt_path = Path(block.get("worktree", ""))
        # Skip the main checkout — identified by path matching git_cwd
        if wt_path == git_cwd:
            continue
        branch: str | None = block.get("branch")
        locked = "locked" in block
        is_bare = "bare" in block
        entries.append(
            WorktreeEntry(path=wt_path, branch=branch, locked=locked, is_bare=is_bare)
        )

    return entries


def check_pr_state(
    branch: str, timeout: int = 10, *, cwd: Path | None = None
) -> tuple[str | None, int | None, bool]:
    """Return (state, pr_number, gh_available) for the most recent PR on *branch*.

    Calls ``gh pr list --head <branch> --state all --json state,number --limit 1``.

    Args:
        branch: The branch name to look up.
        timeout: Seconds before the gh call is killed.
        cwd: Working directory for the gh subprocess — gh infers the repo from CWD.
             Pass the repo root so gh finds the right remote regardless of the
             process's own CWD.

    Returns:
      (state, pr_number, True)  state is "MERGED", "OPEN", "CLOSED", or "" (no PRs)
      (None, None, True)        on transient error (timeout, non-zero exit, bad JSON)
      (None, None, False)       when gh binary is not found
    """
    try:
        result = _sp.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                _GH_PR_LIST_STATE_ALL,
                "--json",
                "state,number",
                "--limit",
                _GH_PR_LIST_LIMIT,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError:
        return None, None, False
    except (OSError, _sp.TimeoutExpired) as exc:
        _log.warning("check_pr_state: gh call failed for %s: %s", branch, exc)
        return None, None, True

    if result.returncode != 0:
        _log.warning(
            "check_pr_state: gh exited %d for %s: %s",
            result.returncode,
            branch,
            result.stderr.strip(),
        )
        return None, None, True

    try:
        data: list[dict[str, object]] = json.loads(result.stdout)
    except (ValueError, AttributeError) as exc:
        _log.warning("check_pr_state: JSON parse error for %s: %s", branch, exc)
        return None, None, True

    if not data:
        return "", None, True

    state = str(data[0].get("state", ""))
    raw_number = data[0].get("number")
    pr_number: int | None = int(str(raw_number)) if raw_number is not None else None
    return state, pr_number, True


def _has_unpushed_commits(wt_path: Path, branch: str) -> bool:
    """Return True if *wt_path* has commits not yet pushed to origin/<branch>.

    Conservative: returns True on any subprocess error or when the remote ref
    does not exist (meaning we can't confirm the branch is fully pushed).
    """
    try:
        result = _sp.run(
            ["git", "-C", str(wt_path), "log", f"origin/{branch}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_clean_env(),
        )
    except (OSError, FileNotFoundError):
        return True  # conservative: treat as unpushed when git is unavailable
    if result.returncode != 0:
        # Remote ref may not exist or git failed — treat as unpushed conservatively.
        return True
    return bool(result.stdout.strip())


def _is_dirty(wt_path: Path, branch: str) -> bool:
    """Return True if *wt_path* has uncommitted changes or unpushed commits.

    Checks both:
    - Uncommitted changes (``git status --porcelain`` is non-empty)
    - Unpushed commits (``git log origin/<branch>..HEAD`` is non-empty)

    Conservative: returns True on any subprocess error so that a git failure
    never allows a destructive removal to proceed.
    """
    try:
        result = _sp.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_clean_env(),
        )
    except (OSError, FileNotFoundError):
        return True  # conservative for destructive operation
    if result.stdout.strip():
        return True
    return _has_unpushed_commits(wt_path, branch)


def _verdict_for_state(
    state: str | None,
    wt_path: Path,
    branch: str,
    *,
    include_closed: bool,
) -> GcVerdict:
    """Map gh PR state to a GcVerdict, calling _is_dirty only when needed."""
    if state == _GH_PR_STATE_OPEN:
        return GcVerdict.KEEP_OPEN_PR
    if state not in (_GH_PR_STATE_MERGED, _GH_PR_STATE_CLOSED):
        # "" (no PR) or None (transient error) — keep conservatively
        return GcVerdict.KEEP_NO_PR
    if state == _GH_PR_STATE_CLOSED and not include_closed:
        return GcVerdict.KEEP_CLOSED_PR
    # MERGED, or CLOSED with include_closed=True — check for unsaved work first
    if _is_dirty(wt_path, branch):
        return GcVerdict.SKIP_DIRTY
    if state == _GH_PR_STATE_MERGED:
        return GcVerdict.REMOVE_MERGED
    return GcVerdict.REMOVE_CLOSED


def classify_worktrees(
    git_cwd: Path,
    timeout: int = 10,
    *,
    include_closed: bool = False,
    worktree_base: Path | None = None,
) -> list[WorktreeGcResult]:
    """Classify all non-main worktrees in the repo at *git_cwd*.

    Args:
        git_cwd: Path to the main git checkout.
        timeout: Seconds per ``gh`` CLI call.
        include_closed: When True, CLOSED PRs are candidates for removal.
        worktree_base: When given, only worktrees whose path is under this
            directory are classified. Others are silently skipped. Used to
            restrict GC to cw-managed worktrees.

    Verdicts:
        - Bare → SKIP_BARE (no PR lookup)
        - Locked → SKIP_LOCKED (no PR lookup)
        - Detached HEAD → SKIP_DETACHED
        - gh unavailable → SKIP_GH_UNAVAILABLE
        - PR MERGED, dirty → SKIP_DIRTY
        - PR MERGED, clean → REMOVE_MERGED
        - PR CLOSED, include_closed=True, clean → REMOVE_CLOSED
        - PR CLOSED, include_closed=True, dirty → SKIP_DIRTY
        - PR CLOSED, include_closed=False → KEEP_CLOSED_PR
        - PR OPEN → KEEP_OPEN_PR
        - No PR or transient error → KEEP_NO_PR (conservative)
    """
    entries = list_repo_worktrees(git_cwd)
    results: list[WorktreeGcResult] = []
    _gh_seen_unavailable = False

    for entry in entries:
        if worktree_base is not None and not entry.path.is_relative_to(worktree_base):
            continue

        if entry.is_bare:
            results.append(
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_BARE, pr_number=None
                )
            )
            continue

        if entry.locked:
            _log.info("gc: skip locked worktree %s", entry.path)
            results.append(
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_LOCKED, pr_number=None
                )
            )
            continue

        if entry.branch is None:
            results.append(
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_DETACHED, pr_number=None
                )
            )
            continue

        if _gh_seen_unavailable:
            results.append(
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_GH_UNAVAILABLE, pr_number=None
                )
            )
            continue

        state, pr_number, gh_available = check_pr_state(
            entry.branch, timeout=timeout, cwd=git_cwd
        )

        if not gh_available:
            _gh_seen_unavailable = True
            results.append(
                WorktreeGcResult(
                    entry=entry, verdict=GcVerdict.SKIP_GH_UNAVAILABLE, pr_number=None
                )
            )
            continue

        verdict = _verdict_for_state(
            state, entry.path, entry.branch, include_closed=include_closed
        )
        if verdict == GcVerdict.SKIP_DIRTY:
            _log.info(
                "gc: skip dirty worktree %s (branch=%s)", entry.path, entry.branch
            )
        results.append(
            WorktreeGcResult(entry=entry, verdict=verdict, pr_number=pr_number)
        )

    return results


def remove_worktree_gc(
    entry: WorktreeEntry,
    git_cwd: Path,
    *,
    delete_branch: bool = True,
) -> bool:
    """Remove *entry*'s worktree and optionally delete its local branch.

    Returns True when the worktree was successfully removed, False otherwise.
    Branch deletion failure does not affect the return value — the worktree
    is gone, only the local ref cleanup failed (logged as a warning).

    Uses ``git worktree remove --force`` then ``git branch -D`` (force-delete).
    Branch deletion is skipped when worktree removal fails to avoid leaving
    git in an inconsistent state (branch gone but worktree still registered).

    Why --force: squash-merged branches are never ancestors of main, so git's
    built-in uncommitted-changes protection would refuse even clean worktrees.
    The caller must verify there is no unsaved work before calling this function.
    """
    clean_env = _git_clean_env()

    wt_result = _sp.run(
        ["git", "worktree", "remove", "--force", str(entry.path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(git_cwd),
        env=clean_env,
    )
    if wt_result.returncode != 0:
        _log.warning(
            "remove_worktree_gc: worktree remove failed for %s (rc=%d): %s",
            entry.path,
            wt_result.returncode,
            wt_result.stderr.strip(),
        )
        return False

    _log.info(
        "remove_worktree_gc: removed worktree %s (branch=%s)", entry.path, entry.branch
    )

    if delete_branch and entry.branch is not None:
        branch_result = _sp.run(
            ["git", "branch", _GIT_BRANCH_DELETE_FLAG, entry.branch],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(git_cwd),
            env=clean_env,
        )
        if branch_result.returncode != 0:
            _log.warning(
                "remove_worktree_gc: branch delete failed for %s (rc=%d): %s",
                entry.branch,
                branch_result.returncode,
                branch_result.stderr.strip(),
            )
        else:
            _log.info("remove_worktree_gc: deleted branch %s", entry.branch)

    return True


def run_worktree_gc(
    git_cwd: Path,
    *,
    apply: bool = False,
    timeout: int = 10,
    include_closed: bool = False,
    worktree_base: Path | None = None,
) -> WorktreeGcReport:
    """GC worktrees for the repo at *git_cwd*.

    Classifies all non-main worktrees via PR state, then optionally removes
    those with REMOVE_* verdicts when *apply* is True.

    Args:
        git_cwd: Path to the main git checkout (not a worktree).
        apply: When True, remove worktrees with REMOVE_* verdicts. Dry-run by default.
        timeout: Seconds per ``gh`` CLI call.
        include_closed: When True, also remove worktrees for CLOSED (abandoned) PRs.
        worktree_base: When given, restrict GC to worktrees under this path.

    Returns:
        WorktreeGcReport with classification results for all worktrees.
    """
    results = classify_worktrees(
        git_cwd,
        timeout=timeout,
        include_closed=include_closed,
        worktree_base=worktree_base,
    )
    report = WorktreeGcReport(results=results)

    if apply:
        for gc_result in report.to_remove:
            if not remove_worktree_gc(gc_result.entry, git_cwd):
                report.removal_failures += 1

    return report
