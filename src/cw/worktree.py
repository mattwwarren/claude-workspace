"""Git worktree operations for isolated session workspaces."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from cw.exceptions import WorktreeError

if TYPE_CHECKING:
    from cw.models import ClientConfig


# cmux rejects worktree names longer than this; the full path is treated
# as the name. Any default layout that would exceed this limit falls
# back to a hash-derived short base under ~/.cw/wt/ so path length stays
# bounded regardless of client name or workspace nesting depth.
_WORKTREE_NAME_CAP = 64
_HASH_BASE_SEGMENTS = (".cw", "wt")
_WORKSPACE_HASH_CHARS = 8


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a filesystem-safe slug.

    Slashes become hyphens: ``feat/search`` -> ``feat-search``.
    """
    return re.sub(r"[/\\]+", "-", branch).strip("-")


def _git_dir(client: ClientConfig) -> Path:
    """Return the directory to use as git cwd for a client.

    Worktree-mode clients use ``repo_path`` (the real clone);
    legacy clients use ``workspace_path``.
    """
    return client.repo_path or client.workspace_path


def resolve_worktree_base(client: ClientConfig) -> Path:
    """Return the worktree base directory for a client.

    Uses ``client.worktree_base`` if set, otherwise defaults to
    ``<git_dir.parent>/.worktrees/<git_dir.name>``.
    """
    if client.worktree_base:
        return client.worktree_base
    ws = _git_dir(client)
    return ws.parent / ".worktrees" / ws.name


def _hashed_worktree_base(client: ClientConfig) -> Path:
    """Return a short hash-derived worktree base for a client.

    Used as a fallback when the default sibling layout would exceed
    ``_WORKTREE_NAME_CAP``. Hash is stable per git directory so repeat
    invocations resolve to the same location.
    """
    git_dir = _git_dir(client)
    digest = hashlib.sha256(str(git_dir).encode("utf-8")).hexdigest()
    return Path.home().joinpath(*_HASH_BASE_SEGMENTS, digest[:_WORKSPACE_HASH_CHARS])


def worktree_path_for(client: ClientConfig, branch: str) -> Path:
    """Return the full worktree path for a branch.

    Falls back to a hash-derived short base under ``~/.cw/wt/`` when the
    default layout would produce a path longer than cmux's 64-char
    worktree-name cap. An explicit ``client.worktree_base`` is always
    honoured, even if it produces a path over the cap — user choice
    wins over the safety net.
    """
    slug = slugify_branch(branch)
    base = resolve_worktree_base(client)
    candidate = base / slug
    if client.worktree_base is not None or len(str(candidate)) <= _WORKTREE_NAME_CAP:
        return candidate
    return _hashed_worktree_base(client) / slug


def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory."""
    cmd = ["git", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            cwd=str(cwd),
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        msg = f"Git command failed: {' '.join(cmd)}\n{stderr}"
        raise WorktreeError(msg) from e


def create_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
) -> Path:
    """Create a git worktree for the given branch.

    Returns the worktree path. Idempotent: returns existing path if already created.
    """
    wt_path = worktree_path_for(client, branch)
    git_cwd = _git_dir(client)

    if wt_path.exists():
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if branch exists locally (refs/heads/ avoids matching tags)
    result = _run_git(
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}",
        cwd=git_cwd,
        check=False,
    )
    if result.returncode == 0:
        # Branch exists — create worktree from it
        args = ["worktree", "add", str(wt_path), branch]
    else:
        # Branch doesn't exist — create new branch
        args = ["worktree", "add", "-b", branch, str(wt_path)]

    if force:
        args.insert(2, "--force")

    _run_git(*args, cwd=git_cwd)

    # Initialize submodules if the repo uses them
    if (git_cwd / ".gitmodules").exists():
        _run_git(
            "submodule",
            "update",
            "--init",
            "--recursive",
            cwd=wt_path,
            check=False,
        )

    return wt_path


def remove_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
) -> None:
    """Remove a git worktree for the given branch."""
    wt_path = worktree_path_for(client, branch)

    if not wt_path.exists():
        return

    args = ["worktree", "remove", str(wt_path)]
    if force:
        args.append("--force")

    _run_git(*args, cwd=_git_dir(client))
