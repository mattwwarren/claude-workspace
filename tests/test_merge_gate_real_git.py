"""Real-git proof of the mechanism Step 4a's merge gate relies on (#2431).

``auto-dev-finalize.md`` Step 4a escalates a surviving file overlap to
``git merge-tree --write-tree <pr-head> <branch-head>`` and trusts its exit
code: 0 means the two heads merge cleanly, 1 means a genuine textual
conflict. These tests prove both claims against the installed git, so a git
upgrade that changed the contract would turn this file red rather than
silently turning the merge gate into a no-op.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from tests.conftest import _clean_git_env, commit_tracked_file, git_in

_FILE = "pyproject.toml"

# Two sections far enough apart that edits to each are disjoint hunks.
_BASE_LINES = [
    "[project]",
    'name = "demo"',
    *[f"# filler {n}" for n in range(12)],
    "[tool.mypy]",
    "strict = true",
]


def _render(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _with(index: int, value: str) -> str:
    lines = list(_BASE_LINES)
    lines[index] = value
    return _render(lines)


def _merge_tree(repo: Path, left: str, right: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "merge-tree", "--write-tree", left, right],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_git_env(),
    )


def _two_branches(make_git_repo: Callable[[str], Path], ours: str, theirs: str) -> Path:
    repo = make_git_repo("merge-gate")
    commit_tracked_file(repo, _FILE, _render(_BASE_LINES))
    git_in(repo, "checkout", "-q", "-b", "theirs")
    commit_tracked_file(repo, _FILE, theirs)
    git_in(repo, "checkout", "-q", "main")
    git_in(repo, "checkout", "-q", "-b", "ours")
    commit_tracked_file(repo, _FILE, ours)
    return repo


def test_same_file_disjoint_hunks_merge_tree_exits_zero(
    make_git_repo: Callable[[str], Path],
) -> None:
    """Shape 2 from the ticket: same file, different sections — not a conflict."""
    repo = _two_branches(
        make_git_repo,
        ours=_with(1, 'name = "demo-renamed"'),
        theirs=_with(len(_BASE_LINES) - 1, "strict = false"),
    )
    result = _merge_tree(repo, "theirs", "ours")
    assert result.returncode == 0, result.stdout + result.stderr


def test_same_file_overlapping_hunks_merge_tree_exits_one(
    make_git_repo: Callable[[str], Path],
) -> None:
    repo = _two_branches(
        make_git_repo,
        ours=_with(1, 'name = "ours"'),
        theirs=_with(1, 'name = "theirs"'),
    )
    result = _merge_tree(repo, "theirs", "ours")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CONFLICT" in result.stdout
