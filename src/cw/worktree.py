"""Git worktree operations for isolated session workspaces."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from cw.exceptions import MissingWorkspaceError, StaleWorktreeError, WorktreeError

if TYPE_CHECKING:
    from cw.models import ClientConfig

_log = logging.getLogger(__name__)


# The native spawn backend (``spawn_create_impl`` / ``claude --bg`` with
# ``cwd=``) has no path-length restriction beyond OS limits (PATH_MAX ~4096).
# The 64-char threshold is a conservative trigger: for any realistic workspace
# path the default candidate exceeds this cap, so the hash-fallback base
# (``~/.cw/wt/``) is used in practice — keeping paths short and predictable.
_WORKTREE_NAME_CAP = 64
_HASH_BASE_SEGMENTS = (".cw", "wt")
# 8 hex chars = 32 bits. For a single-user tool with a handful of
# clients the collision probability is negligible; raising this value
# pushes the hashed base closer to _WORKTREE_NAME_CAP and reduces
# headroom for the branch slug, so increase with care.
_WORKSPACE_HASH_CHARS = 8


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a worktree-safe slug.

    Collapses any run of disallowed characters into a single hyphen, then
    strips leading/trailing hyphens. The allowed charset (``[A-Za-z0-9._-]``)
    matches ``claude -w``'s worktree-name validator — anything outside this
    set (path separators, ``#``, spaces, unicode) becomes ``-``.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


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
    ``_WORKTREE_NAME_CAP``. The hash seeds from the *resolved* git
    directory so symlinks and non-canonical paths collapse to the
    same digest — ``create_worktree`` and ``remove_worktree`` must
    agree on the location across invocations.
    """
    git_dir = _git_dir(client).resolve()
    digest = hashlib.sha256(str(git_dir).encode("utf-8")).hexdigest()
    return Path.home().joinpath(*_HASH_BASE_SEGMENTS, digest[:_WORKSPACE_HASH_CHARS])


def worktree_path_for(client: ClientConfig, branch: str) -> Path:
    """Return the full worktree path for a branch.

    Falls back to a hash-derived short base under ``~/.cw/wt/`` when the
    default layout would produce a path longer than the 64-char path-length
    threshold. An explicit ``client.worktree_base`` is always honoured, even
    if it produces a path over the threshold — user choice wins over the
    safety net.
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


def create_worktree(
    client: ClientConfig,
    branch: str,
    *,
    force: bool = False,
) -> Path:
    """Create a git worktree for the given branch.

    Returns the worktree path. Idempotent: returns the existing path when it is
    already a worktree on *branch*. A pre-existing directory checked out on a
    *different* branch (or not a worktree at all) is treated as stale and
    raises :exc:`StaleWorktreeError` rather than being reused (see below).
    """
    wt_path = worktree_path_for(client, branch)
    git_cwd = _git_dir(client)

    check_not_main_checkout(wt_path, client)

    if wt_path.exists():
        # Idempotent reuse is only safe when the existing worktree is still on
        # the branch we were asked for. A stale worktree left by a prior failed
        # dispatch (crash before reconcile's TIMED_OUT cleanup, see #404) can
        # carry a different branch — and thus a prior run's commits — into the
        # new session. Silently reusing it feeds the worker the wrong context
        # and has caused cross-ticket isolation breaches (#402). Refuse on
        # mismatch: the dispatch loop reverts the task to PENDING and reconcile
        # removes the stale tree so the retry starts clean.
        current_branch = _checked_out_branch(wt_path)
        if current_branch != branch:
            found = current_branch or "(none / detached HEAD / not a worktree)"
            msg = (
                f"Refusing to reuse stale worktree at {wt_path}: expected "
                f"branch {branch!r} but found {found}. Remove it with "
                f"`git worktree remove --force {wt_path}`, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
        if worktree_has_unsaved_work(client, branch):
            msg = (
                f"Refusing to reuse worktree at {wt_path} for branch {branch!r}: "
                f"it has unsaved work (uncommitted changes or unpushed commits). "
                f"Commit or push the work, then re-dispatch."
            )
            raise StaleWorktreeError(msg)
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


def worktree_has_unsaved_work(client: ClientConfig, branch: str) -> bool:
    """Return True if the worktree for *branch* has unsaved work.

    "Unsaved" means either:
    - uncommitted changes (``git status --porcelain`` is non-empty), OR
    - unpushed commits (``git log origin/<branch>..HEAD`` is non-empty).

    Returns False when the worktree path does not exist (nothing to lose).
    When ``origin/<branch>`` does not exist, treats all HEAD commits as
    unpushed (conservative: assume they would be lost).

    Never raises — every git error is swallowed and logged at WARNING level
    so that a git failure cannot block a cleanup sweep.
    """
    wt_path = worktree_path_for(client, branch)
    if not wt_path.exists():
        return False

    # 1. Uncommitted changes check
    try:
        status = _run_git("status", "--porcelain", cwd=wt_path, check=False)
        if status.stdout.strip():
            return True
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: status check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work so we don't silently destroy.
        return True

    # 2. Unpushed commits check — compare HEAD against origin/<branch>.
    # First verify that origin/<branch> exists; if not, every HEAD commit is
    # "unpushed" (conservative).
    try:
        ref_check = _run_git(
            "rev-parse",
            "--verify",
            f"origin/{branch}",
            cwd=wt_path,
            check=False,
        )
        if ref_check.returncode != 0:
            # origin/<branch> unknown — check whether HEAD has any commits
            head_check = _run_git(
                "rev-parse", "--verify", "HEAD", cwd=wt_path, check=False
            )
            return head_check.returncode == 0 and bool(head_check.stdout.strip())
        log_result = _run_git(
            "log",
            f"origin/{branch}..HEAD",
            "--oneline",
            cwd=wt_path,
            check=False,
        )
        return bool(log_result.stdout.strip())
    except (WorktreeError, OSError) as exc:
        _log.warning(
            "worktree_has_unsaved_work: log check failed for %s/%s: %s",
            client.name,
            branch,
            exc,
        )
        # Fail-safe: treat as having unsaved work.
        return True


def _fetch_default_branch(client_name: str, default_branch: str, git_dir: Path) -> bool:
    """Fetch origin/<default_branch>. Returns True on success, False on failure."""
    if not git_dir.exists():
        _log.warning(
            "freshness_check_skip: workspace missing for %s (%s)",
            client_name,
            git_dir,
        )
        return False
    try:
        result = _run_git(
            "fetch", "origin", default_branch, "--quiet", cwd=git_dir, check=False
        )
    except (WorktreeError, FileNotFoundError, PermissionError) as exc:
        _log.warning(
            "freshness_check_skip: %s (%s): %s",
            client_name,
            git_dir,
            exc,
        )
        return False
    if result.returncode != 0:
        _log.warning(
            "freshness_check_skip: fetch failed for %s (rc=%d): %s",
            client_name,
            result.returncode,
            result.stderr.strip(),
        )
        return False
    return True


def fetch_feature_branch(client: ClientConfig, branch_name: str) -> bool:
    """Fetch origin/<branch_name> into the client's git directory.

    Resolves the stale-local-ref problem described in GitHub issue #381:
    when the impl agent pushes commits from an isolation worktree, the
    parent worktree's local ref for the feature branch is not updated.
    Calling this before computing ``git diff FORK_POINT...origin/<branch>``
    for reviewer prompts ensures the diff reflects the actual pushed state.

    Returns True on success, False on any failure (fetch errors do not raise).
    """
    return _fetch_default_branch(client.name, branch_name, _git_dir(client))


def _get_behind_count(
    client_name: str, default_branch: str, git_dir: Path
) -> tuple[str, str, int] | None:
    """Get (local_sha, origin_sha, behind_count). Returns None on failure."""
    try:
        local_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
        origin_sha = _run_git(
            "rev-parse", f"origin/{default_branch}", cwd=git_dir
        ).stdout.strip()
        behind_count = int(
            _run_git(
                "rev-list",
                "--count",
                f"{default_branch}..origin/{default_branch}",
                cwd=git_dir,
            ).stdout.strip()
        )
    except (WorktreeError, ValueError):
        _log.warning(
            "is_main_behind_origin: rev-parse/rev-list failed for %s", client_name
        )
        return None
    else:
        return (local_sha, origin_sha, behind_count)


def is_main_behind_origin(
    client: ClientConfig,
) -> tuple[bool, str, str, int]:
    """Check whether the client's local default branch is behind origin.

    Fetches ``origin/<default_branch>`` then compares local and remote SHAs.

    Returns:
        A 4-tuple ``(is_stale, local_sha, origin_sha, behind_count)`` where
        *is_stale* is ``True`` when the local branch is behind the remote.
        On any fetch or parse failure returns ``(False, "", "", 0)`` and logs
        a WARNING — the caller should treat failure as non-stale.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    if not _fetch_default_branch(client.name, default_branch, git_dir):
        return (False, "", "", 0)

    counts = _get_behind_count(client.name, default_branch, git_dir)
    if counts is None:
        return (False, "", "", 0)

    local_sha, origin_sha, behind_count = counts
    return (behind_count > 0, local_sha, origin_sha, behind_count)


def fast_forward_main(client: ClientConfig) -> tuple[str, str]:
    """Fast-forward the client's local default branch to origin.

    Runs ``git pull --ff-only origin <default_branch>`` in the client's git
    directory.  Raises :exc:`MissingWorkspaceError` if the workspace directory
    does not exist, or :exc:`WorktreeError` if the pull fails (non-zero exit)
    or if the checkout is not on ``default_branch`` or has uncommitted changes
    — both conditions risk mutating the index unexpectedly (#428).

    Returns:
        ``(before_sha, after_sha)`` — the SHA before and after the pull.
        When already up to date both values are equal.
    """
    git_dir = _git_dir(client)
    if not git_dir.exists():
        msg = f"workspace missing for {client.name} ({git_dir})"
        raise MissingWorkspaceError(msg)
    default_branch = client.default_branch

    # Guard 1: ensure the checkout is on the expected default branch.
    current_branch = _run_git(
        "symbolic-ref", "--short", "HEAD", cwd=git_dir
    ).stdout.strip()
    if current_branch != default_branch:
        msg = (
            f"Refusing to fast-forward {client.name}: HEAD is on "
            f"'{current_branch}', expected '{default_branch}'. "
            f"Switch to '{default_branch}' before refreshing."
        )
        raise WorktreeError(msg)

    # Guard 2: ensure the working tree is clean.
    status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout.strip()
    if status_out:
        msg = (
            f"Refusing to fast-forward {client.name}: working tree is dirty "
            f"(git status --porcelain reported changes). "
            f"Commit or stash changes before refreshing."
        )
        raise WorktreeError(msg)

    before_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    _run_git("pull", "--ff-only", "origin", default_branch, cwd=git_dir)
    after_sha = _run_git("rev-parse", default_branch, cwd=git_dir).stdout.strip()
    return (before_sha, after_sha)
