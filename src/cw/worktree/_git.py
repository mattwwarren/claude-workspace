"""Shared git-subprocess leaf helpers for :mod:`cw.worktree`.

Every other ``cw.worktree`` submodule runs git through :func:`_run_git` and
resolves a client's git directory through :func:`_git_dir`.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from cw.exceptions import WorktreeError

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig


# Git porcelain v1 format: "XY path" — 2-char status prefix + 1 space = 3 chars.
_GIT_PORCELAIN_PATH_OFFSET = 3
# XY field value for untracked files in porcelain v1.
_GIT_PORCELAIN_UNTRACKED = "??"


def _git_dir(client: ClientConfig) -> Path:
    """Return the directory to use as git cwd for a client.

    Worktree-mode clients use ``repo_path`` (the real clone);
    legacy clients use ``workspace_path``.
    """
    return client.repo_path or client.workspace_path


def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory.

    Strips ``GIT_*`` from the environment so cw's git operations target
    the client repo at *cwd* and never inherit a parent process's repo
    selection. Without this, running cw from inside a git hook (e.g. a
    pre-commit pytest run) would leak ``GIT_DIR`` / ``GIT_INDEX_FILE``
    into the subprocess and produce confusing "Not a directory" errors.
    """
    cmd = ["git", *args]
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            cwd=str(cwd),
            env=clean_env,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        msg = f"Git command failed: {' '.join(cmd)}\n{stderr}"
        raise WorktreeError(msg) from e


def _ref_exists(ref: str, git_cwd: Path) -> bool:
    """Return True if *ref* resolves to a valid object in *git_cwd*."""
    result = _run_git("rev-parse", "--verify", ref, cwd=git_cwd, check=False)
    return result.returncode == 0


def check_not_main_checkout(worktree_path: Path, client: ClientConfig) -> None:
    """Raise WorktreeError if *worktree_path* resolves to the client's main checkout.

    Guards against the #300 regression: a degenerate path where a worktree
    resolves to the main checkout, causing git commits to land there instead of
    the intended branch worktree.  Uses Path.resolve() to catch symlinks.
    """
    main_checkout = _git_dir(client)
    if worktree_path.resolve() == main_checkout.resolve():
        msg = (
            f"Refusing to operate on main checkout: worktree path {worktree_path} "
            f"resolves to the same location as the client's main checkout "
            f"({main_checkout}). A prior 'git worktree add' likely targeted "
            f"the main repo directory instead of a new branch worktree."
        )
        raise WorktreeError(msg)


def _checked_out_branch(wt_path: Path) -> str | None:
    """Return the branch checked out in *wt_path*, or None.

    None means *wt_path* is not a registered git worktree or is in
    detached-HEAD state (``git branch --show-current`` prints nothing or
    exits non-zero), or git itself could not be invoked. Never raises — the
    idempotent-reuse guard in :func:`create_worktree` treats every None as a
    refuse-to-reuse signal, so swallowing an ``OSError`` here (e.g. a missing
    git binary) is correct: the worktree cannot be trusted either way.
    """
    try:
        result = _run_git("branch", "--show-current", cwd=wt_path, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _first_line(text: str) -> str:
    """Return the first line of *text* ('' when empty) for one-line log fields."""
    lines = text.strip().splitlines()
    return lines[0] if lines else ""
