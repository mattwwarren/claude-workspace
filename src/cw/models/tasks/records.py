"""Small persisted record models carried on ``TicketTask``.

``PendingFixDispatch``, ``LaunchedFixWorker``, ``UsageLimitAct`` and
``MustFixOverride``. Pydantic and the standard library only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class PendingFixDispatch(BaseModel):
    """Durable action-list handoff for the review fix loop (GitHub #2017 R21).

    Written by the REVIEW session under ``dev_queue_lock()`` before it exits;
    consumed by ``cw.reconcile.fix_dispatch`` on a later reconcile tick, from a
    process that is never resident in the ticket's worktree. That separation is
    the whole point: ``cw.spawn._write_hook_context``'s DAEMON guard refuses any
    spawn into a worktree whose ``cw-context.json`` references a still-live
    session, so a review session can never dispatch a fix session into its own
    worktree. By the time this record is consumed the writing session is
    terminal by construction, and the guard is satisfied without an exemption.

    Carries the prompt TEXT, not a path: the prompt *is* the action list, and a
    worktree-local file is not a durable surface (R21.4) — it dies with the
    worktree. Lives in ``dev_queue.json``, which outlives both.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    prompt: str
    label: str
    cycle: int
    requested_by_session_id: str
    requested_at: datetime


class LaunchedFixWorker(BaseModel):
    """Tombstone of a fix-loop worker that launched but was never recorded.

    (GitHub #2590, refs #2502.) Stamped by ``cw.reconcile.fix_dispatch`` when
    ``dispatch_fix_agent`` raises ``WorkerLaunchedError``: the worker is live in
    the daemon roster, but a later spawn step (usually the ``sessions.json``
    write) failed, so no session row may exist to say when it finishes. While
    this is set, the completions phase holds the row RUNNING until a readable
    roster no longer lists *surface_ref*, so a fresh REVIEW worker is never
    started over the fix worker. ``cw.reconcile.fix_dispatch_hold`` is its
    only reader and clears it once the roster confirms the worker stopped.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    # The worker's daemon roster short id (``claude stop <surface_ref>``).
    surface_ref: str
    # When the tombstone was stamped; the page grace counts from here.
    launched_at: datetime
    # When the unconfirmed-worker page last succeeded; None until the first.
    attention_paged_at: datetime | None = None


class UsageLimitAct(BaseModel):
    """Write-ahead intent for reconcile's mid-turn usage-limit act (GitHub #2324).

    The act spans four stores that cannot commit together: the daemon, the
    session state, the dev queue and the event inbox. So the decision is this
    one row write, made under ``dev_queue_lock`` after the side-effect-free
    gate; every step after it is idempotent and any reconcile tick that finds
    the intent resumes it (``cw.reconcile.usage_limit_mid_turn``). The row's
    final transition clears it in the same write, and so does any other
    transition (``transition_task_status``), which ends the act.

    While it is set, the generic phantom, completed-session backstop and
    liveness paths leave the row alone, so an interrupted act is never charged
    an attempt.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    session_id: str
    branch: Literal["auto", "park"]
    started_at: datetime
    # The instant parsed from the limit message, or None when it named none
    # that could be resolved; kept only for the lockout audit's provenance.
    reset_at: datetime | None
    # When the lockout lifts and the requeued row becomes claimable again:
    # reset_at when usable, else started_at plus the flat backoff. Fixed at
    # decision time so every resume uses the same window.
    until: datetime
    # Set once the act's audit events are recorded, so a resume does not
    # re-emit them. An emit that lands just before a crash is re-emitted: the
    # audit is at-least-once, keyed by (session_id, started_at).
    audited_at: datetime | None = None


class MustFixOverride(BaseModel):
    """Operator override of a codex MUST_FIX park (GitHub #2205).

    Bound to the verdict it was given for: ``reviewed_sha`` and the
    ``cw.review_debt.fingerprint_v1`` identity of every MUST_FIX finding on it.
    FINALIZE's ``check_must_fix_override.py`` honors it only while the live
    verdict still carries that SHA and finding set and HEAD has not moved.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    actor: str
    reason: str
    reviewed_sha: str
    finding_ids: list[tuple[str, str]]
    recorded_at: datetime
