"""Monkeypatch-stable indirection for cross-cutting external dependencies.

These callables are each used by more than one reconcile cluster (idle,
stalled, phantom, salvage, tasks, core). Cluster modules invoke them as
``_deps.NAME(...)`` rather than importing them directly, so a single test
patch at ``cw.reconcile._deps.NAME`` intercepts every caller regardless of
which cluster module makes the call. Before the package split these all lived
in the single ``reconcile`` module namespace, where one ``cw.reconcile.NAME``
patch covered every call site; this module preserves that single-patch-point
property.

``host_timezone`` is exported without the leading underscore its underlying
``cw.native_daemon`` helper carries so that cluster modules can reference it
as a public attribute without tripping the private-member-access lint.
``checked_out_branch`` is defined here: a strict, reconcile-local branch
lookup for the worktree dirty check (#2662), not an alias of
``cw.worktree._checked_out_branch``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw._git import git_clean_env
from cw.config import load_effective_clients
from cw.gh import branch_exists_on_origin, fetch_branch_prs, pr_is_merged_for_ticket
from cw.native_daemon import _host_timezone as host_timezone
from cw.native_daemon import get_native_daemon_client, read_supervisor_resume_session_id
from cw.notify import fire_push_notification

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BranchLookupError",
    "branch_exists_on_origin",
    "checked_out_branch",
    "fetch_branch_prs",
    "fire_push_notification",
    "get_native_daemon_client",
    "host_timezone",
    "load_effective_clients",
    "pr_is_merged_for_ticket",
    "read_supervisor_resume_session_id",
]

# Longest first stderr line a BranchLookupError keeps.
_STDERR_DETAIL_MAX = 120
# The stderr marker (under ``LC_ALL=C``) of a directory git does not see as a
# repository: a plain directory, or a ``.git`` file naming a removed gitdir.
_NOT_A_REPO = "not a git repository"


class BranchLookupError(Exception):
    """``git branch --show-current`` failed for an unrecognized reason (#2662).

    Any non-zero exit whose stderr does not say "not a git repository":
    dubious ownership, a bad git config, a git too old for ``--show-current``,
    or anything else. Those can be host-wide, so the dirty check defers on it
    instead of reading the worktree unresolved. A plain ``Exception``, not a
    ``CwError``, so no broad ``except CwError`` swallows it.
    """

    def __init__(self, returncode: int, stderr_line: str) -> None:
        self.returncode = returncode
        self.stderr_line = stderr_line[:_STDERR_DETAIL_MAX]
        super().__init__(returncode, self.stderr_line)

    def __str__(self) -> str:
        return f"git branch --show-current exited {self.returncode}: {self.stderr_line}"


def checked_out_branch(wt_path: Path) -> str | None:
    """Return the branch checked out at *wt_path*, or None if it is unresolved.

    Strict, reconcile-local lookup for the worktree dirty check (#2662); its
    only consumer is ``cw.reconcile._shared._worktree_evidence``. It classifies
    ``git branch --show-current`` five ways:

    1. exit 0 with a name: that branch (an unborn branch lands here too);
    2. exit 0 with no output: detached HEAD, so ``None``;
    3. non-zero with "not a git repository" on stderr (a plain directory, or a
       ``.git`` file naming a removed gitdir): ``None``;
    4. any other non-zero exit: raises :class:`BranchLookupError`;
    5. ``OSError`` (missing or non-running git, unusable cwd, fork failure)
       propagates unchanged.

    It differs from ``cw.worktree._checked_out_branch``, which reads every
    failure as ``None``, because a missing git or a host-global git failure
    must not read as an unresolved branch. It does not use ``_run_git``, which
    accepts no ``env``: this lookup keeps its own ``subprocess.run`` call and
    composes :func:`cw._git.git_clean_env` (every ``GIT_*`` variable stripped,
    so an enclosing git hook cannot redirect it) with ``LC_ALL=C`` so the
    stderr match is not localized. Output is decoded as UTF-8 with
    ``errors="replace"``: a git ref name is bytes, and a strict decode would
    raise an unclassified ``UnicodeDecodeError`` out of ``reconcile()``. A
    replacement-decoded name is still a non-empty branch, so the dirty check
    goes on to the unsaved-work probe (fail closed).
    """
    env = {**git_clean_env(), "LC_ALL": "C"}
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(wt_path),
        env=env,
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    if _NOT_A_REPO in result.stderr:
        return None
    first_line = result.stderr.strip().partition("\n")[0]
    raise BranchLookupError(result.returncode, first_line)
