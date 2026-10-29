"""The review diff base: one value the loop and the confirm both diff from (#2800).

A clean-mode review diffs the branch in two places: the loop rounds (the
Claude-native prose, or the codex fix loop) and the independent confirm round
(``cw review confirm``, or the codex lane's Claude confirm). They must review
the same change set. The ledger's ``fork_point`` cannot be that base: the
prose computes it once at Checkpoint 2, so after a later merge of
``origin/<default>`` it is still a valid ancestor of HEAD yet its diff carries
every commit the merge brought in. ``git merge-base origin/<default> HEAD`` is
the base, and ``git diff <it>...HEAD`` is byte-identical to ``git diff
origin/<default>...HEAD``.

- :func:`resolve_diff_base` computes it from the local tracking ref.
- :func:`refresh_diff_base` fetches the default branch first: the one
  fetch-then-resolve entry point, shared by ``cw review converge diff-base`` and
  ``cw review confirm``'s back-fill of a ledger written before #2800, so the two
  cannot drift.
- :func:`verify_diff_base` fails closed unless a pinned value is a commit, an
  ancestor of HEAD, and still the live merge-base (a pin from before a merge of
  the default branch is stale).

Every failure is a :class:`~cw.exceptions.CwError` (exit 1 at the CLI): an
unverifiable base is a cw or invocation fault, never an outage. Import these
names from :mod:`cw.review_convergence`, not from this private submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw._git import run_git
from cw.exceptions import CwError
from cw.review_convergence._release import _NOT_ANCESTOR_EXIT
from cw.review_convergence._render import one_line

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Sequence
    from pathlib import Path

#: The remedy every pin failure names: re-pin at the current HEAD.
_REPIN_HINT = "run `cw review converge diff-base`"


def _git(argv: Sequence[str], worktree: Path) -> subprocess.CompletedProcess[str]:
    """``git *argv`` in *worktree*, captured; a ``CwError`` when git cannot run."""
    try:
        return run_git(argv, cwd=worktree, capture_output=True)
    except OSError as exc:
        msg = f"git {' '.join(argv)} could not run in {worktree}: {exc}"
        raise CwError(msg) from exc


def resolve_diff_base(worktree: Path, default_branch: str, *, ref: str = "HEAD") -> str:
    """``git merge-base origin/<default_branch> <ref>`` in *worktree*.

    Reads the local ``origin/<default_branch>`` tracking ref as it stands (no
    fetch; see :func:`refresh_diff_base`). Raises ``CwError`` on a non-zero
    exit (an unresolvable ref included), on empty output, or when git cannot
    run.
    """
    command = f"git merge-base origin/{default_branch} {ref}"
    result = _git(["merge-base", f"origin/{default_branch}", ref], worktree)
    if result.returncode != 0:
        msg = f"{command} failed in {worktree}: {one_line(result.stderr)}"
        raise CwError(msg)
    sha = result.stdout.strip()
    if not sha:
        msg = f"{command} printed no commit"
        raise CwError(msg)
    return sha


def refresh_diff_base(worktree: Path, default_branch: str) -> str:
    """Fetch ``origin/<default_branch>``, then return the live merge-base with HEAD.

    The branch is fetched by name, as ``converge verify-tip`` fetches its own,
    so the remote's default refspec updates the tracking ref the merge-base
    reads. A stale ``origin/<default>`` would reintroduce the bug, so a fetch
    failure raises ``CwError`` rather than falling back to the local ref. No
    ``timeout`` is passed: cw imposes no kill timer (ADR-0014).
    """
    fetched = _git(["fetch", "origin", default_branch], worktree)
    if fetched.returncode != 0:
        msg = (
            f"git fetch origin {default_branch} failed in {worktree}: "
            f"{one_line(fetched.stderr)}"
        )
        raise CwError(msg)
    return resolve_diff_base(worktree, default_branch)


def _require_ancestor(worktree: Path, sha: str) -> None:
    """``CwError`` unless *sha* is an ancestor of HEAD (exit 1 means "not")."""
    result = _git(["merge-base", "--is-ancestor", sha, "HEAD"], worktree)
    if result.returncode == 0:
        return
    if result.returncode == _NOT_ANCESTOR_EXIT:
        msg = f"diff base {sha} is not an ancestor of HEAD; {_REPIN_HINT}"
        raise CwError(msg)
    msg = (
        f"git merge-base --is-ancestor failed in {worktree} with exit "
        f"{result.returncode}: {one_line(result.stderr)}"
    )
    raise CwError(msg)


def verify_diff_base(worktree: Path, diff_base: str | None, default_branch: str) -> str:
    """The pinned *diff_base*, verified against git, or a ``CwError``.

    Fails closed unless *diff_base* is set, resolves to a commit, is an
    ancestor of HEAD, and equals the live :func:`resolve_diff_base` (no fetch:
    the caller pinned it moments ago). A pin that is still a valid ancestor
    but predates a merge of the default branch is the original #2800 bug, so
    it is rejected as stale rather than silently re-pinned; an unresolvable
    ``origin/<default>`` fails closed too. Returns the full commit sha.
    """
    if diff_base is None:
        msg = f"the ledger records no diff base; {_REPIN_HINT}"
        raise CwError(msg)
    commit = _git(
        ["rev-parse", "--verify", "--quiet", f"{diff_base}^{{commit}}"], worktree
    )
    sha = commit.stdout.strip()
    if commit.returncode != 0 or not sha:
        msg = f"diff base {diff_base!r} is not a commit in {worktree}"
        raise CwError(msg)
    _require_ancestor(worktree, sha)
    live = resolve_diff_base(worktree, default_branch)
    if live != sha:
        msg = (
            f"diff base {sha} is stale: the merge-base with "
            f"origin/{default_branch} is now {live}; {_REPIN_HINT}"
        )
        raise CwError(msg)
    return sha
