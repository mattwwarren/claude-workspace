"""Exception hierarchy for cw."""

from __future__ import annotations

import re

# Usage-limit detection regex. Matches all documented Claude usage-limit phrasings:
# - "You've hit your session limit · resets 3:45pm"   (verified against errors.md)
# - "You've hit your weekly limit · resets Mon 12:00am" (verified against errors.md)
# - "You've hit your Opus limit · resets 3:45pm"      (verified against errors.md)
# Uses \S+ rather than an explicit allow-list so undocumented future limit types
# (e.g. "5-hour limit") are also detected. Replaces the narrower
# r"hit (?:your )?(?:session|usage) limit" from reconcile.py:126 which missed
# weekly and Opus variants.
USAGE_LIMIT_RE = re.compile(r"hit (?:your )?\S+ limit", re.IGNORECASE)


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


class UsageLimitError(CwError):
    """Raised when ``claude --bg`` fails because a fleet-wide usage limit is active.

    Detection: output matches :data:`USAGE_LIMIT_RE`. Both spawn-time
    (``CalledProcessError`` path) and post-spawn (stdout without session ID) paths
    raise this error.

    Back-off: callers (dispatch loop) set a ``usage_limited_until`` window and skip
    further spawns until it elapses. See :func:`cw.dispatch.run_dispatch_loop`.
    """

    __slots__ = ()
