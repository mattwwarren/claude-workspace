"""Exception hierarchy for cw."""

from __future__ import annotations


class CwError(Exception):
    """Base exception for all cw errors."""

    __slots__ = ()


class WorktreeError(CwError):
    """Error from git worktree operations."""

    __slots__ = ()


class MissingWorkspaceError(WorktreeError):
    """Raised when a client's workspace directory does not exist.

    This is a config-hygiene condition (stale/misconfigured entry), not a git
    operation failure. Callers should treat it as a soft skip rather than an
    error that contributes to exit-1 semantics.
    """

    __slots__ = ()


class StaleWorktreeError(WorktreeError):
    """A pre-existing worktree is checked out on a branch other than requested.

    Raised only by ``create_worktree``'s idempotent-reuse guard (#404). Kept
    distinct from the base :class:`WorktreeError` so the dispatch loop can
    force-remove the stale tree and let the task retry — without conflating it
    with other git failures (notably the main-checkout guard) that must never
    trigger a removal.
    """

    __slots__ = ()


class HookContextConflictError(CwError):
    """A user-owned ``.claude/settings.local.json`` already exists.

    Raised by hook-context injection on a USER-origin session whose
    worktree already carries a settings file. The user-managed file must
    not be silently clobbered with the cw Stop-hook template; callers
    (Phase C of multiplexer-removal) route this to a clean failure mode
    rather than overwriting.
    """

    __slots__ = ()


class DisclaimerNotAcceptedError(CwError):
    """Raised when ``claude --bg`` fails because the user has not accepted
    the bypass-permissions disclaimer.

    Detection: the substring ``"requires accepting the disclaimer first"`` is
    present in ``CalledProcessError.stderr``. Verified against the live
    ``claude`` binary at version 2.1.150 via ``strings`` — the full stderr line
    emitted by that binary is::

        --bg with bypassPermissions requires accepting the disclaimer first.
        Run `claude --dangerously-skip-permissions` once interactively.

    Remediation: run ``claude --dangerously-skip-permissions`` once interactively
    to accept the disclaimer (persisted to ``~/.claude/settings.json`` as
    ``skipDangerousModePermissionPrompt: true``).
    """

    __slots__ = ()
