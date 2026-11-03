"""Shared git-subprocess leaf helpers for :mod:`cw.worktree`.

Every other ``cw.worktree`` submodule runs git through :func:`_run_git` and
resolves a client's git directory through :func:`_git_dir`. The one
``git branch --show-current`` probe lives here too (#2903):
:func:`probe_checked_out_branch` classifies the outcome into a
:data:`BranchProbe`, and :func:`_checked_out_branch` is its lenient
``str | None`` wrapper.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw._git import git_clean_env
from cw.exceptions import WorktreeError

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig


# Git porcelain v1 format: "XY path" — 2-char status prefix + 1 space = 3 chars.
_GIT_PORCELAIN_PATH_OFFSET = 3
# XY field value for untracked files in porcelain v1.
_GIT_PORCELAIN_UNTRACKED = "??"

# Longest first stderr line a BranchLookupError keeps.
_STDERR_DETAIL_MAX = 120
# Allowlist of stderr phrases (under ``LC_ALL=C``) git uses for a directory it
# does not see as a repository: a plain directory, or a ``.git`` file naming a
# removed gitdir or holding malformed content. Git's wording differs across
# versions (CI's git says "gitfile does not point to a valid repository" where
# a newer local git says "not a git repository"), so one phrase is not enough.
# A non-zero exit matching none of these stays possibly host-global. The single
# source of truth for this classification (#2903).
_PER_ROW_REPO_FAULTS = (
    "not a git repository",
    "gitfile does not point to a valid repository",
    "invalid gitfile format",
)


class BranchLookupError(Exception):
    """``git branch --show-current`` failed for an unrecognized reason (#2662).

    Any non-zero exit whose stderr matches no ``_PER_ROW_REPO_FAULTS`` phrase:
    dubious ownership, a bad git config, a git too old for ``--show-current``,
    or anything else. Those can be host-wide, so the reconcile dirty check
    defers on it instead of reading the worktree unresolved. Carried as
    :attr:`ProbeUnavailable.error`, not raised by the probe. A plain
    ``Exception``, not a ``CwError``, so no broad ``except CwError`` swallows
    it if a caller re-raises it.
    """

    def __init__(self, returncode: int, stderr_line: str) -> None:
        self.returncode = returncode
        self.stderr_line = stderr_line[:_STDERR_DETAIL_MAX]
        super().__init__(returncode, self.stderr_line)

    def __str__(self) -> str:
        return f"git branch --show-current exited {self.returncode}: {self.stderr_line}"


@dataclass(frozen=True, slots=True)
class OnBranch:
    """``git branch --show-current`` named a branch (an unborn one included)."""

    name: str


@dataclass(frozen=True, slots=True)
class DetachedHead:
    """``git branch --show-current`` exited 0 with no output: detached HEAD."""


@dataclass(frozen=True, slots=True)
class NotARepository:
    """Git does not see the directory as a repository (a per-row fault).

    A non-zero exit whose stderr carries a ``_PER_ROW_REPO_FAULTS`` phrase: a
    plain directory, or a ``.git`` file naming a removed gitdir or malformed.
    """


@dataclass(frozen=True, slots=True)
class ProbeUnavailable:
    """The probe could not answer, for a possibly host-global reason.

    ``error`` is the :class:`BranchLookupError` for an unrecognized non-zero
    exit, or the ``OSError`` that stopped git from running (missing or
    non-running git, unusable cwd, fork failure), carried unchanged so a
    caller can name it or stat the path.
    """

    error: BranchLookupError | OSError


type BranchProbe = OnBranch | DetachedHead | NotARepository | ProbeUnavailable


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


def probe_checked_out_branch(wt_path: Path) -> BranchProbe:
    """Classify ``git branch --show-current`` at *wt_path*; never raises.

    The one implementation of the checked-out-branch lookup (#2903), folded
    from the strict reconcile copy (#2662) and the lenient worktree one:

    1. exit 0 with a name: :class:`OnBranch` (an unborn branch lands here too);
    2. exit 0 with no output: :class:`DetachedHead`;
    3. non-zero with a ``_PER_ROW_REPO_FAULTS`` phrase on stderr (a plain
       directory, or a ``.git`` file naming a removed gitdir or malformed):
       :class:`NotARepository`;
    4. any other non-zero exit: :class:`ProbeUnavailable` carrying a
       :class:`BranchLookupError`;
    5. ``OSError`` (missing or non-running git, unusable cwd, fork failure):
       :class:`ProbeUnavailable` carrying that exception.

    It does not use :func:`_run_git`, which accepts no ``env``: it keeps its
    own ``subprocess.run`` call and composes :func:`cw._git.git_clean_env`
    (every ``GIT_*`` variable stripped, so an enclosing git hook cannot
    redirect it) with ``LC_ALL=C`` so the stderr match is not localized.
    Output is decoded as UTF-8 with ``errors="replace"``: a git ref name is
    bytes, and a strict decode would raise an unclassified
    ``UnicodeDecodeError``. A replacement-decoded name is still a branch.
    """
    env = {**git_clean_env(), "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            cwd=str(wt_path),
            env=env,
        )
    except OSError as exc:
        return ProbeUnavailable(exc)
    if result.returncode == 0:
        name = result.stdout.strip()
        return OnBranch(name) if name else DetachedHead()
    if any(phrase in result.stderr for phrase in _PER_ROW_REPO_FAULTS):
        return NotARepository()
    first_line = result.stderr.strip().partition("\n")[0]
    return ProbeUnavailable(BranchLookupError(result.returncode, first_line))


def _checked_out_branch(wt_path: Path) -> str | None:
    """Return the branch checked out in *wt_path*, or None.

    Lenient wrapper over :func:`probe_checked_out_branch`: None means
    *wt_path* is in detached-HEAD state, is not a git repository, or git
    could not answer (an unrecognized exit, or an ``OSError`` such as a
    missing git binary). Never raises — the idempotent-reuse guard in
    :func:`create_worktree` treats every None as a refuse-to-reuse signal, so
    reading every failure as None is correct: the worktree cannot be trusted
    either way. Callers that must tell those failures apart use the probe.
    """
    match probe_checked_out_branch(wt_path):
        case OnBranch(name):
            return name
        case _:
            return None


def _first_line(text: str) -> str:
    """Return the first line of *text* ('' when empty) for one-line log fields."""
    lines = text.strip().splitlines()
    return lines[0] if lines else ""
