"""Worktree GC: remove worktrees for squash-merged or closed branches via PR state."""

from __future__ import annotations

import enum
import json
import logging
import os
import subprocess as _sp
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

# git worktree list --porcelain field prefixes
_PORCELAIN_WORKTREE = "worktree "
_PORCELAIN_BRANCH = "branch refs/heads/"
_PORCELAIN_LOCKED = "locked"
_PORCELAIN_DETACHED = "detached"

# gh CLI subcommand args
_GH_PR_LIST_STATE_ALL = "all"
_GH_PR_LIST_LIMIT = "1"

# gh PR state values returned in JSON output
_GH_PR_STATE_MERGED = "MERGED"
_GH_PR_STATE_CLOSED = "CLOSED"
_GH_PR_STATE_OPEN = "OPEN"

# Why -D not -d: squash-merged branches are never ancestors of main, so -d
# (safe delete) always refuses them. We check PR state before removing, so
# force-delete here is safe.
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
    SKIP_LOCKED = "SKIP_LOCKED"
    SKIP_GH_UNAVAILABLE = "SKIP_GH_UNAVAILABLE"
    SKIP_DETACHED = "SKIP_DETACHED"
    SKIP_DIRTY = "SKIP_DIRTY"


# Canonical verdict partitions — single source of truth for all three report properties
# and any formatter. Adding a new GcVerdict requires updating exactly one set here.
GC_REMOVE_VERDICTS: frozenset[GcVerdict] = frozenset(
    {GcVerdict.REMOVE_MERGED, GcVerdict.REMOVE_CLOSED}
)
GC_KEEP_VERDICTS: frozenset[GcVerdict] = frozenset(
    {GcVerdict.KEEP_OPEN_PR, GcVerdict.KEEP_NO_PR}
)
GC_SKIP_VERDICTS: frozenset[GcVerdict] = frozenset(
    {
        GcVerdict.SKIP_LOCKED,
        GcVerdict.SKIP_GH_UNAVAILABLE,
        GcVerdict.SKIP_DETACHED,
        GcVerdict.SKIP_DIRTY,
    }
)


@dataclass(frozen=True)
class WorktreeEntry:
    """A single git worktree parsed from porcelain output."""

    path: Path
    branch: str | None  # None means detached HEAD
    locked: bool


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

    @property
    def to_remove(self) -> list[WorktreeGcResult]:
        """Results that should be (or were) removed."""
        return [r for r in self.results if r.verdict in GC_REMOVE_VERDICTS]

    @property
    def kept(self) -> list[WorktreeGcResult]:
        """Results that are kept (open PR or no PR)."""
        return [r for r in self.results if r.verdict in GC_KEEP_VERDICTS]

    @property
    def skipped(self) -> list[WorktreeGcResult]:
        """Results that were skipped (locked, dirty, detached, or gh unavailable)."""
        return [r for r in self.results if r.verdict in GC_SKIP_VERDICTS]


def _parse_worktree_blocks(stdout: str) -> list[dict[str, str]]:
    """Parse porcelain worktree output into a list of field dicts.

    Each block is separated by a blank line. Fields are key-value where
    ``worktree``, ``HEAD``, ``branch`` have a value; ``locked`` and
    ``detached`` are bare flags (value may be a reason string).
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
        elif stripped.startswith("HEAD "):
            current["HEAD"] = stripped[5:]
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
        entries.append(WorktreeEntry(path=wt_path, branch=branch, locked=locked))

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


def _is_dirty(wt_path: Path) -> bool:
    """Return True if *wt_path* has uncommitted changes."""
    try:
        result = _sp.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_clean_env(),
        )
    except (OSError, FileNotFoundError):
        return False
    return bool(result.stdout.strip())


def _verdict_for_state(
    state: str | None,
    wt_path: Path,
    *,
    include_closed: bool,
) -> GcVerdict:
    """Map gh PR state to a GcVerdict, calling _is_dirty only when needed."""
    if state == _GH_PR_STATE_MERGED:
        return GcVerdict.SKIP_DIRTY if _is_dirty(wt_path) else GcVerdict.REMOVE_MERGED
    if state == _GH_PR_STATE_CLOSED:
        if not include_closed:
            return GcVerdict.KEEP_NO_PR
        return GcVerdict.SKIP_DIRTY if _is_dirty(wt_path) else GcVerdict.REMOVE_CLOSED
    if state == _GH_PR_STATE_OPEN:
        return GcVerdict.KEEP_OPEN_PR
    # "" (no PR) or None (transient error) — keep conservatively
    return GcVerdict.KEEP_NO_PR


def classify_worktrees(
    git_cwd: Path,
    timeout: int = 10,
    *,
    include_closed: bool = False,
) -> list[WorktreeGcResult]:
    """Classify all non-main worktrees in the repo at *git_cwd*.

    For each worktree:
    - Locked → SKIP_LOCKED (gh never called)
    - Detached HEAD → SKIP_DETACHED
    - gh unavailable → SKIP_GH_UNAVAILABLE
    - PR MERGED, dirty → SKIP_DIRTY
    - PR MERGED, clean → REMOVE_MERGED
    - PR CLOSED, include_closed=True, clean → REMOVE_CLOSED
    - PR CLOSED, include_closed=True, dirty → SKIP_DIRTY
    - PR CLOSED, include_closed=False → KEEP_NO_PR
    - PR OPEN → KEEP_OPEN_PR
    - No PR or transient error → KEEP_NO_PR (conservative)
    """
    entries = list_repo_worktrees(git_cwd)
    results: list[WorktreeGcResult] = []
    _gh_seen_unavailable = False

    for entry in entries:
        if entry.locked:
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

        verdict = _verdict_for_state(state, entry.path, include_closed=include_closed)
        results.append(
            WorktreeGcResult(entry=entry, verdict=verdict, pr_number=pr_number)
        )

    return results


def remove_worktree_gc(
    entry: WorktreeEntry,
    git_cwd: Path,
    *,
    delete_branch: bool = True,
) -> None:
    """Remove *entry*'s worktree and optionally delete its local branch.

    Uses ``git worktree remove --force`` then ``git branch -D`` (force-delete).
    Branch deletion is skipped when worktree removal fails to avoid leaving
    git in an inconsistent state (branch gone but worktree still registered).
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
        return

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


def run_worktree_gc(
    git_cwd: Path,
    *,
    apply: bool = False,
    timeout: int = 10,
    include_closed: bool = False,
) -> WorktreeGcReport:
    """GC worktrees for the repo at *git_cwd*.

    Classifies all non-main worktrees via PR state, then optionally removes
    those with REMOVE_* verdicts when *apply* is True.

    Args:
        git_cwd: Path to the main git checkout (not a worktree).
        apply: When True, remove worktrees with REMOVE_* verdicts. Dry-run by default.
        timeout: Seconds per ``gh`` CLI call.
        include_closed: When True, also remove worktrees for CLOSED (abandoned) PRs.

    Returns:
        WorktreeGcReport with classification results for all worktrees.
    """
    results = classify_worktrees(
        git_cwd, timeout=timeout, include_closed=include_closed
    )
    report = WorktreeGcReport(results=results)

    if apply:
        for gc_result in report.to_remove:
            remove_worktree_gc(gc_result.entry, git_cwd)

    return report
