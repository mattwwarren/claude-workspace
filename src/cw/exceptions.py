"""Exception hierarchy for cw."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.sprint import AppliedBuildout

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


class LaneMoveError(CwError):
    """Raised when a ticket cannot be moved due to its current status."""

    __slots__ = ()


class LaneNotFoundError(CwError):
    """Raised when a target lane is not declared for the client."""

    __slots__ = ()


class ConfigValidationError(CwError):
    """A config-facing Pydantic model (``ClientConfig``, ``OrchestratorConfig``,
    etc.) failed validation while loading ``clients.yaml`` or
    ``orchestrator.yaml`` (GitHub #1200).

    Raised by :func:`cw.config.load_clients` and
    :func:`cw.config.load_orchestrator_config`, wrapping the underlying
    ``pydantic.ValidationError`` so callers (the CLI boundary's
    ``handle_errors``, the dispatch loop's guarded config reload, ``cw
    doctor``'s loader-failure checks) can catch one ``CwError`` subclass
    instead of reaching across the pydantic import boundary. The message
    names the offending file and, via the wrapped pydantic error text, the
    specific field/key that failed -- e.g. an ``extra="forbid"`` rejection of
    a typo'd config key.
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


class SpawnUnregisteredError(CwError):
    """Raised when a spawned worker never appears in the daemon roster.

    After ``claude --bg`` returns a short session id, cw polls the daemon
    roster to verify the supervisor actually adopted the worker. When the id
    is absent after the polling window elapses, this error is raised instead
    of leaving a phantom RUNNING session that burns a 30-minute idle cycle
    before the watchdog reaps it.

    The caller (dispatch loop) handles it the same as any broad spawn
    failure: revert the task to PENDING for retry. A distinct
    ``SESSION_SPAWN_UNREGISTERED`` event is emitted before the raise so the
    failure is diagnosable in the event inbox.
    """

    __slots__ = ()


class ApproveGateError(CwError):
    """Raised when a ticket cannot be approved because it is not at an approval gate."""

    __slots__ = ()


class EmitValidationError(CwError):
    """Raised by emit_result_locked() when the payload fails AutoDevResult
    validation. Carries the formatted field-error lines (see _format_errors
    in cw.result) so the cw result emit CLI wrapper can reproduce the
    existing 'field.path: message' stderr lines byte-identically without
    reaching back across the pydantic import boundary.
    """

    __slots__ = ("errors",)

    def __init__(self, message: str, *, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


class EmitSessionNotFoundError(CwError):
    """Raised by emit_result_locked() when the resolved session_id has no
    matching session in state. Carries the session id so callers (the CLI
    wrapper) can reconstruct the "Session '<id>' not found; no state was
    modified." message without re-deriving it.
    """

    __slots__ = ("session_id",)

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class RequeueStateError(CwError):
    """Raised when a ticket cannot be requeued because it is not BLOCKED_ON_USER."""

    __slots__ = ()


class RequeueStageError(CwError):
    """Raised when requeue would regress a ticket to an earlier stage."""

    __slots__ = ()


class UnblockStateError(CwError):
    """Raised when a ticket cannot be unblocked because it is not park-marked."""

    __slots__ = ()


class DispatchServeError(CwError):
    """Raised when the dispatch supervisor exhausts its restart budget.

    Raised instead of ``sys.exit`` so the CLI boundary (``handle_errors``)
    owns the process-exit decision and programmatic callers get a catchable
    signal rather than a hard process kill.
    """

    __slots__ = ()


class VersionDriftError(DispatchServeError):
    """Raised when the dispatch loop detects it is running stale code.

    Caught by :func:`run_dispatch_serve` to trigger a clean restart without
    counting toward the crash cap — a version reload is intentional, not a
    crash.
    """

    __slots__ = ()


class DispatchLoopLockedError(CwError):
    """Raised when a dispatch loop is launched while another already holds the
    process-lifetime singleton lock (GitHub #1362).

    ``run_dispatch_loop`` acquires an advisory, non-blocking ``fcntl.flock``
    over ``DISPATCH_LOOP_LOCK`` at entry. A second launch (via ``cw dev-queue
    run`` or ``cw dev-queue serve``, including ``run --once``) fails fast with
    this error, whose message names the holding process's PID and normalized
    command so the operator can stop it or re-launch with ``--force``. A plain
    :class:`CwError` subclass — deliberately NOT a :class:`DispatchServeError`
    — so :func:`run_dispatch_serve` re-raises it immediately instead of
    swallowing it into its crash-restart/backoff loop.
    """

    __slots__ = ()


class RfcContractError(CwError):
    """An RFC does not satisfy the buildout input contract.

    Raised by :func:`cw.sprint.parse_rfc` when a required section or ticket
    field is absent, or when a ticket cites a decision/ticket/epic that the RFC
    does not define. The message always names the exact defect (e.g. "missing
    section: ## Tickets") so the operator can fix the RFC rather than guess.
    """

    __slots__ = ()


class SprintApplyError(CwError):
    """Raised when :func:`cw.sprint.apply_plan` cannot complete a `gh`
    issue-creation pass — any milestone/epic/ticket lookup or create call
    that reports failure (``ok=False`` or a ``None`` return).

    Carries the partial ``AppliedBuildout`` state accumulated before the
    failure via ``applied``, so the operator can see exactly what was already
    created or skipped and re-run ``cw sprint apply`` to resume rather than
    starting over (creation is idempotent by title). The type is only
    available under ``TYPE_CHECKING`` — ``cw.sprint`` imports from
    ``cw.exceptions``, so importing ``AppliedBuildout`` at runtime here would
    cycle; the annotation is deferred (``from __future__ import annotations``)
    so this never executes at import time.
    """

    __slots__ = ("applied",)

    def __init__(self, message: str, *, applied: AppliedBuildout | None = None) -> None:
        super().__init__(message)
        self.applied = applied


class SessionsLockReentryError(CwError):
    """Raised when ``sessions_lock()`` is re-entered on the same thread while
    already held.

    ``sessions_lock`` is a per-open-fd ``fcntl.flock``, which is not
    reentrant: a second acquisition on the same thread blocks forever in
    ``flock()`` against the fd already held by the outer acquisition
    (GitHub #1228 — the review-recipe act phase transitively re-entering
    ``reconcile()`` from inside its own locked body). Raising here, guarded
    by a thread-local flag checked before any second ``flock()`` syscall,
    converts that hang into a catchable error. Existing callers on the
    reentrant paths (``_dispatch_auto_fix_ci``, ``_dispatch_address_review``,
    ``_reconcile_usage_limited``) already catch ``CwError`` / broad
    ``Exception`` around the call that would otherwise re-enter, so no
    call-site changes are needed elsewhere.
    """

    __slots__ = ()
