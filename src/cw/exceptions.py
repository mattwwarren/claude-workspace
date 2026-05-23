"""Exception hierarchy for cw."""

from __future__ import annotations


class CwError(Exception):
    """Base exception for all cw errors."""


class WorktreeError(CwError):
    """Error from git worktree operations."""


class HookContextConflictError(CwError):
    """A user-owned ``.claude/settings.local.json`` already exists.

    Raised by hook-context injection on a USER-origin session whose
    worktree already carries a settings file. The user-managed file must
    not be silently clobbered with the cw Stop-hook template; callers
    (Phase C of multiplexer-removal) route this to a clean failure mode
    rather than overwriting.
    """
