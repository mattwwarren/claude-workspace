"""Git worktree operations for isolated session workspaces."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
# Git porcelain v1 format: "XY path" — 2-char status prefix + 1 space = 3 chars.
_GIT_PORCELAIN_PATH_OFFSET = 3
# XY field value for untracked files in porcelain v1.
_GIT_PORCELAIN_UNTRACKED = "??"
# 8 hex chars = 32 bits. For a single-user tool with a handful of
# clients the collision probability is negligible; raising this value
# pushes the hashed base closer to _WORKTREE_NAME_CAP and reduces
# headroom for the branch slug, so increase with care.
_WORKSPACE_HASH_CHARS = 8
# Pattern appended to $GIT_COMMON_DIR/info/exclude so ephemeral per-session
# .cw/ artifacts are invisible to git status without touching .gitignore.
_CW_EXCLUDE_PATTERN = ".cw/"


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


def _has_commits_beyond_base(wt_path: Path) -> bool:
    """Return True iff the worktree has commits beyond origin/main.

    Runs git log origin/main..HEAD in the worktree cwd. Returns False on
    any failure — conservative default so uncertainty never triggers salvage.

    # Why: salvage is a side-effecting external write. A false positive
    # (salvaging a session with no real commits) is worse than a false
    # negative (missing a salvageable session). Fail safe to False.
    """
    if not wt_path.exists():
        return False
    try:
        result = _run_git(
            "log",
            "origin/main..HEAD",
            "--oneline",
            cwd=wt_path,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _register_cw_exclude(git_cwd: Path) -> None:
    """Idempotently append .cw/ to $GIT_COMMON_DIR/info/exclude.

    Uses git rev-parse --git-common-dir so the write targets the shared
    object-store directory even when called from within a worktree. Never
    touches the committed .gitignore. Logs a warning and returns on any
    git or I/O failure rather than propagating — exclude registration is
    advisory and must not abort worktree creation.
    """
    try:
        result = _run_git("rev-parse", "--git-common-dir", cwd=git_cwd)
        common_dir_str = result.stdout.strip()
        if not common_dir_str:
            _log.warning(
                "_register_cw_exclude: empty --git-common-dir output in %s", git_cwd
            )
            return
        common_dir = (
            Path(common_dir_str)
            if Path(common_dir_str).is_absolute()
            else git_cwd / common_dir_str
        )
        exclude_path = common_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        if _CW_EXCLUDE_PATTERN in existing.splitlines():
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a") as fh:
            fh.write(f"{separator}{_CW_EXCLUDE_PATTERN}\n")
    except (WorktreeError, OSError) as exc:
        _log.warning("_register_cw_exclude: failed for %s: %s", git_cwd, exc)


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
    _register_cw_exclude(git_cwd)

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


def _has_unpushed_commits(client: ClientConfig, branch: str, wt_path: Path) -> bool:
    """Return True if *branch* has commits not on any known base ref.

    Three-level fallback:
    1. ``origin/<branch>`` — canonical; used when the branch was pushed.
    2. ``origin/<default_branch>`` — fallback when branch has no remote yet.
    3. Local ``<default_branch>`` — offline / bare-clone fallback.
    Returns True conservatively on subprocess failure or all-refs-absent.
    """
    try:
        ref_check = _run_git(
            "rev-parse",
            "--verify",
            f"origin/{branch}",
            cwd=wt_path,
            check=False,
        )
        if ref_check.returncode == 0:
            # Level 1: origin/<branch> exists — canonical happy path.
            log_result = _run_git(
                "log",
                f"origin/{branch}..HEAD",
                "--oneline",
                cwd=wt_path,
                check=False,
            )
            return bool(log_result.stdout.strip())

        # Level 2: origin/<branch> absent — compare against origin/<default_branch>.
        default_base = f"origin/{client.default_branch}"
        log_result = _run_git(
            "log",
            f"{default_base}..HEAD",
            "--oneline",
            cwd=wt_path,
            check=False,
        )
        if log_result.returncode == 0:
            return bool(log_result.stdout.strip())

        # Level 3: origin/<default_branch> also absent (offline / bare clone) —
        # fall back to local default branch ref.
        log_result = _run_git(
            "log",
            f"{client.default_branch}..HEAD",
            "--oneline",
            cwd=wt_path,
            check=False,
        )
        if log_result.returncode == 0:
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
    # All refs unresolvable — conservative fail-safe.
    return True


def worktree_has_unsaved_work(client: ClientConfig, branch: str) -> bool:
    """Return True if the worktree for *branch* has unsaved work.

    "Unsaved" means either:
    - uncommitted changes (``git status --porcelain`` is non-empty), OR
    - unpushed commits (``git log origin/<branch>..HEAD`` is non-empty).

    Returns False when the worktree path does not exist (nothing to lose).
    When ``origin/<branch>`` does not exist, falls back to comparing against
    ``origin/<default_branch>`` then the local ``<default_branch>`` ref. If
    all refs are unresolvable (e.g. offline), returns True conservatively.

    Never raises — every git error is swallowed and logged at WARNING level
    so that a git failure cannot block a cleanup sweep.
    """
    wt_path = worktree_path_for(client, branch)
    if not wt_path.exists():
        return False

    # 1. Uncommitted changes check
    try:
        status = _run_git("status", "--porcelain", cwd=wt_path, check=False)
        # Filter out cw's own artifacts (.claude/) — these are written fresh
        # each session and would otherwise trip the dirty check on every retry.
        # Porcelain format: "XY path" (2-char status + space + path). Rename
        # entries ("R  old -> new") pass through unchanged; cw artifacts never
        # appear as renames so they will still be caught by path check.
        lines = [
            line
            for line in status.stdout.splitlines()
            if not (
                len(line) > _GIT_PORCELAIN_PATH_OFFSET
                and line[_GIT_PORCELAIN_PATH_OFFSET:].startswith(".claude/")
            )
        ]
        if lines:
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

    # 2. Unpushed commits check — three-level fallback when remote tracking
    # branch is absent (see _has_unpushed_commits for the strategy).
    return _has_unpushed_commits(client, branch, wt_path)


def _fetch_default_branch(
    client_name: str,
    default_branch: str,
    git_dir: Path,
    warned_fetch_fail: set[str] | None = None,
) -> bool:
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
        if warned_fetch_fail is None or client_name not in warned_fetch_fail:
            stderr = result.stderr.strip()
            first_line = stderr.splitlines()[0] if stderr else ""
            _log.warning(
                "freshness_check_skip: fetch failed for %s (rc=%d): %s",
                client_name,
                result.returncode,
                first_line,
            )
            if warned_fetch_fail is not None:
                warned_fetch_fail.add(client_name)
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
    warned_fetch_fail: set[str] | None = None,
) -> tuple[bool, str, str, int]:
    """Check whether the client's local default branch is behind origin.

    Fetches ``origin/<default_branch>`` then compares local and remote SHAs.

    Args:
        client: Client configuration.
        warned_fetch_fail: Caller-owned set of client names that have already
            received a fetch-failure WARNING in this run. Suppresses repeated
            WARNINGs for the same client across ticks. Pass ``None`` (default)
            to always log (correct for one-shot callers).

    Returns:
        A 4-tuple ``(is_stale, local_sha, origin_sha, behind_count)`` where
        *is_stale* is ``True`` when the local branch is behind the remote.
        On any fetch or parse failure returns ``(False, "", "", 0)`` and logs
        a WARNING — the caller should treat failure as non-stale.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    if not _fetch_default_branch(
        client.name, default_branch, git_dir, warned_fetch_fail=warned_fetch_fail
    ):
        return (False, "", "", 0)

    counts = _get_behind_count(client.name, default_branch, git_dir)
    if counts is None:
        return (False, "", "", 0)

    local_sha, origin_sha, behind_count = counts
    return (behind_count > 0, local_sha, origin_sha, behind_count)


def check_main_ff_safety(
    client: ClientConfig,
) -> Literal["equal", "behind", "ahead", "diverged", "detached"]:
    """Classify local main's relationship to origin for dispatch auto-ff.

    Returns one of:
      "behind"   — local main is strictly behind origin; fast-forward is safe
      "equal"    — local main matches origin; no action needed
      "ahead"    — local main has unpushed commits; operator action required
      "diverged" — local main has both new commits and is behind; needs reconciliation
      "detached" — HEAD is detached; fast-forward would be unsafe

    Operative outcomes from the dispatch path (when stale=True is already
    established): "behind" triggers auto-ff; "diverged" and "detached" fall
    through to TICKET_NEEDS_SYNC + warn. "equal" and "ahead" exist for
    defensive completeness but are not reachable from the stale=True path.
    """
    git_dir = _git_dir(client)
    default_branch = client.default_branch

    # Detached HEAD check — symbolic-ref exits non-zero when detached.
    # Prior art: _checked_out_branch() at line 148; fast_forward_main() below.
    sym = _run_git("symbolic-ref", "--short", "HEAD", cwd=git_dir, check=False)
    if sym.returncode != 0:
        return "detached"

    # Two merge-base --is-ancestor calls for directional classification.
    main_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        default_branch,
        f"origin/{default_branch}",
        cwd=git_dir,
        check=False,
    )
    origin_behind = _run_git(
        "merge-base",
        "--is-ancestor",
        f"origin/{default_branch}",
        default_branch,
        cwd=git_dir,
        check=False,
    )
    # returncode 0 means the first arg is a reachable ancestor of the second.
    is_main_ancestor = main_behind.returncode == 0  # main ≤ origin → behind
    is_origin_ancestor = origin_behind.returncode == 0  # origin ≤ main → ahead

    if is_main_ancestor and is_origin_ancestor:
        return "equal"
    if is_main_ancestor:
        return "behind"
    if is_origin_ancestor:
        return "ahead"
    return "diverged"


def fast_forward_main(
    client: ClientConfig, *, ignore_untracked: bool = False
) -> tuple[str, str]:
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

    # Guard 2: ensure the working tree is clean (or only has untracked files).
    status_out = _run_git("status", "--porcelain", cwd=git_dir).stdout
    status_lines = status_out.splitlines()
    if ignore_untracked:
        # Why: dispatch auto-ff may run against a workspace with untracked runtime
        # artifacts (.claude/scheduled_tasks.lock etc.); git pull --ff-only is
        # safe with untracked files because ff-only never rewrites the working tree.
        status_lines = [
            line for line in status_lines if line[:2] != _GIT_PORCELAIN_UNTRACKED
        ]
    if status_lines:
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
