"""Git worktree operations for isolated session workspaces."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from cw.exceptions import WorktreeError

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig


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


def worktree_path_for(client: ClientConfig, branch: str) -> Path:
    """Return the full worktree path for a branch."""
    base = resolve_worktree_base(client)
    return base / slugify_branch(branch)


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


def list_worktrees(client: ClientConfig) -> list[dict[str, str]]:
    """List all git worktrees for the client's repo.

    Returns a list of dicts with ``path`` and ``branch`` keys.
    """
    result = _run_git(
        "worktree",
        "list",
        "--porcelain",
        cwd=_git_dir(client),
        check=False,
    )
    if result.returncode != 0:
        return []

    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            # Strip refs/heads/ prefix
            current["branch"] = ref.removeprefix("refs/heads/")
        elif not line.strip() and current:
            worktrees.append(current)
            current = {}
    if current:
        worktrees.append(current)
    return worktrees
