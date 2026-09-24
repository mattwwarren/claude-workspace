"""Push-and-verify for the codex fix loop's commits (#2354).

``_commit_fix_cycle`` used to commit each fix cycle and return, pushing
nothing. A fix commit that existed only in the local worktree was then at the
mercy of whatever a later stage happened to do: finalize's reset-to-origin
discarded one (#2216), and a session exit with it unpushed parked the ticket
``dirty_worktree`` (#2324). The Claude-native fix agent is told to push with an
explicit refspec and verify the tip (``auto-dev-review.md``); this module is
the same contract for the pure-Python codex path, which no agent prompt
reaches.

Lives beside :mod:`cw.codex_fix_loop` rather than inside it for the same reason
:mod:`cw.codex_fix_loop_convergence` does: that module is already over the
repo's module-size ceiling. Uses the raw ``subprocess`` idiom the rest of the
codex fix-loop family uses, not ``cw.worktree._run_git``.

Every failure surfaces as ``subprocess.CalledProcessError`` — the real one from
``git push``, or a synthetic one for a failed tip verification — so the fix
loop's existing ``except subprocess.CalledProcessError`` parks it unchanged.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw._git import git_clean_env

if TYPE_CHECKING:
    from pathlib import Path

#: Prefix on a verify-mismatch error's stderr, so a diagnostics reader never
#: mistakes a tip mismatch for a failed ``git push``.
SYNTHETIC_MISMATCH_PREFIX = "SYNTHETIC (tip mismatch after push, not a git failure):"
_SYNTHETIC_DETACHED = (
    "SYNTHETIC (detached HEAD, not a git failure): no checked-out branch to push"
)


def remote_branch_tip(worktree: Path, branch: str) -> str | None:
    """Fetch *branch* from origin and return ``origin/<branch>``'s sha.

    ``None`` when the fetch fails (unreachable origin, or a branch that was
    never pushed) or the tracking ref does not resolve afterward. A failed
    fetch never falls back to a stale local tracking ref: a stale ref equal to
    HEAD would falsely report the branch as pushed.
    """
    fetch = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=git_clean_env(),
    )
    if fetch.returncode != 0:
        return None
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=git_clean_env(),
    )
    return resolved.stdout.strip() or None


def push_and_verify_head(worktree: Path, expected_sha: str) -> None:
    """Push HEAD to ``origin/<branch>`` and verify origin now tips at *expected_sha*.

    Raises ``subprocess.CalledProcessError`` when the push fails (carrying
    git's own stdout/stderr), when HEAD is detached, or when the fetched
    ``origin/<branch>`` does not equal *expected_sha* after a successful push.
    """
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"],
        cwd=worktree,
        text=True,
        env=git_clean_env(),
    ).strip()
    if not branch:
        raise subprocess.CalledProcessError(
            1,
            ["git", "branch", "--show-current"],
            output="",
            stderr=_SYNTHETIC_DETACHED,
        )
    subprocess.run(
        ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
        env=git_clean_env(),
    )
    origin_sha = remote_branch_tip(worktree, branch)
    if origin_sha != expected_sha:
        raise subprocess.CalledProcessError(
            1,
            ["git", "rev-parse", f"origin/{branch}"],
            output="",
            stderr=(
                f"{SYNTHETIC_MISMATCH_PREFIX} origin/{branch}={origin_sha} "
                f"expected={expected_sha}"
            ),
        )
