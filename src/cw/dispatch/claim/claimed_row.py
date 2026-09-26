"""Locked mutators for one already-claimed RUNNING dev-queue row.

The shared ``dev_queue_lock()`` load -> re-find -> mutate -> save primitives
that claim-path and post-spawn callers route through: the #2219 identity
re-find (:func:`_find_running_row`), the revert-to-PENDING and
park-BLOCKED_ON_USER transitions, and the spawn-success stamp. Extracted
verbatim from the historical flat ``cw.dispatch.claim`` module by the package
split (#2378).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus, Stage

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")


_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS: int = 2


_SPAWN_ERROR_BACKOFF_CAP_SECONDS: int = 300


def _find_running_row(
    store: DevQueueStore,
    ticket_id: str,
    client_name: str,
    *,
    created_at: datetime | None = None,
    session_id: str | None = None,
) -> TicketTask | None:
    """Return the caller's own RUNNING row for ``(ticket_id, client_name)``.

    The shared locked re-find (#2219) for every site that already holds one
    specific row and must mutate *that* row, not whichever RUNNING row for the
    ticket happens to come first. Duplicate RUNNING rows for one
    ``(ticket_id, client)`` are reachable via add-after-terminal plus
    ``requeue --from-completed``, so ``(ticket_id, client, RUNNING)`` alone is
    not an identity.

    Two identity kinds, AND-combined when both are supplied:

    - ``created_at`` -- fixed at row creation and never reassigned, so it
      works before any session exists (claim-time and pre-spawn callers,
      mirroring :func:`_apply_plan_bypass_if_available`'s #1286 re-find).
    - ``session_id`` -- ``TicketTask.session_id == Session.id`` for a row a
      session has already been stamped onto (post-spawn callers, e.g.
      ``cw.reconcile.codex_boot`` and ``cw.doctor.loop_health``).

    Both default to ``None``, but at least one is required: every production
    caller of every function that routes through this helper (in this
    module, ``cw.codex_background``, and ``cw.doctor.loop_health``) already
    supplies an identity, so a bare ``(ticket_id, client_name, RUNNING)``
    match here would only ever serve a caller that skipped disambiguating a
    duplicate RUNNING row -- the exact hazard #2219 closes. Raises
    :class:`ValueError` rather than silently falling back to that match.

    Read-only: the caller mutates the returned row and saves *store* itself,
    under the ``dev_queue_lock()`` it loaded *store* under.
    """
    if created_at is None and session_id is None:
        msg = (
            "_find_running_row requires created_at or session_id (#2219): "
            "a bare (ticket_id, client_name, RUNNING) match can mutate the "
            "wrong duplicate row"
        )
        raise ValueError(msg)
    for stored_task in store.tasks:
        if (
            stored_task.ticket_id == ticket_id
            and stored_task.client == client_name
            and stored_task.status == QueueItemStatus.RUNNING
            and (created_at is None or stored_task.created_at == created_at)
            and (session_id is None or stored_task.session_id == session_id)
        ):
            return stored_task
    return None


def _revert_claimed_task_to_pending(
    client_name: str,
    ticket_id: str,
    *,
    stamp_backoff: bool = False,
    hook_context_conflict_session_id: str | None = None,
    defer_for: timedelta | None = None,
    expected_session_id: str | None = None,
    created_at: datetime | None = None,
) -> bool:
    """Revert a still-RUNNING claimed task back to PENDING, clearing session_id.

    Returns whether a row was actually reverted: ``False`` when no RUNNING row
    matched, including an ``expected_session_id`` or ``created_at`` mismatch.
    Callers that only revert their own just-failed claim can ignore it; a
    caller that reports the revert (an event, a log line) must gate on it.

    Used by both the usage-limit and broad spawn-error paths: the task was
    claimed to RUNNING by :func:`_claim_next_pending` but spawn never
    succeeded, so it must return to PENDING for a later tick to retry.

    When *stamp_backoff* is True (generic spawn_error path only), increments
    spawn_error_count and sets next_eligible_at to enforce exponential backoff
    before the task is re-claimed.  The usage-limit path passes stamp_backoff=False
    because it has its own fleet-wide backoff mechanism.  See GitHub #868.

    *hook_context_conflict_session_id* (GitHub #1674) mirrors *stamp_backoff*'s
    conditional-stamp shape: it is applied only when the caller supplies a
    real id (the narrow :class:`~cw.exceptions.HookContextConflictError`
    handler), and left untouched otherwise. This function is a FAILURE path —
    the worktree conflict is not known to be resolved just because a later,
    unrelated attempt hit :class:`UsageLimitError` or a generic exception, so
    an unrelated revert must not silently erase still-live conflict evidence.
    (The stamp itself is reset to None only by the successful-spawn path
    below — this function never clears it. The conflicting session going
    terminal or being superseded by id does NOT touch the stamp; it only
    makes concierge recipe 1's refusal predicate evaluate False on the next
    cycle, per docs/events.md's `concierge.hook_context_conflict_refused`
    section. The stamp is stale-but-harmless once the predicate is False —
    the next successful spawn is what actually clears it.)

    *defer_for* (#2213) turns the revert into a RELEASE of the claim, for a
    transient skip in which no spawn was ever attempted (the reused worktree is
    occupied by a live session). It undoes the claim's own charges instead of
    billing a failure: ``task.attempts`` is decremented back to its pre-claim
    value, ``unproductive_attempts`` is not charged (``unproductive=False``),
    and ``spawn_error_count`` is left alone. The row is held off for *defer_for*
    (``next_eligible_at``) so the same tick does not re-claim it. Nothing here
    is a failure, so it must not spend the ticket's attempt budget toward the
    global ceiling, and the caller must not signal ``spawn_error`` either.

    ``expected_session_id`` (optional, #2285) re-verifies
    ``stored_task.session_id == expected_session_id`` under the *same*
    ``dev_queue_lock()`` acquisition that performs the revert, exactly as
    :func:`_park_running_task_blocked_on_user` does. The caller is
    ``cw.reconcile.codex_boot``'s clean-orphan requeue, which decides from an
    unlocked snapshot and then runs git/psutil checks before calling here: a
    row re-claimed by a fresh session in that window must not be reverted out
    from under it, so a mismatch skips the revert silently. The same-tick
    spawn-failure callers omit it -- they revert their own just-failed claim,
    so there is no snapshot to go stale.

    ``created_at`` (#2219) is the same-tick callers' identity instead: they
    pass their claimed task's ``created_at`` so a duplicate RUNNING row for
    the same ``(ticket_id, client)`` is never reverted in their place. Both
    identities route through :func:`_find_running_row`; in every real call
    exactly one of them is supplied.

    # Why: task.attempts is NOT decremented on the FAILURE paths (no
    # *defer_for*). The increment-at-claim contract is intentional —
    # usage_limit deaths and spawn errors consume real dispatch budget and must
    # count toward task.attempts (#786). Those reverts leave task.status
    # RUNNING -> PENDING, which also charges task.unproductive_attempts by
    # default (#1750) since a spawn that never succeeded produced no evidence
    # of progress. The global_attempt_ceiling (this module) reads
    # unproductive_attempts; the corollary #756 stalled-stage cap deliberately
    # still reads raw task.attempts — as of #1750 these are two separate
    # counters, not one shared counter.
    """
    reverted = False
    with dev_queue_lock():
        store = load_dev_queue()
        stored_task = _find_running_row(
            store,
            ticket_id,
            client_name,
            created_at=created_at,
            session_id=expected_session_id,
        )
        if stored_task is not None:
            transition_task_status(
                stored_task,
                QueueItemStatus.PENDING,
                unproductive=defer_for is None,
            )
            reverted = True
            stored_task.session_id = None
            if defer_for is not None:
                stored_task.attempts = max(0, stored_task.attempts - 1)
                stored_task.next_eligible_at = datetime.now(UTC) + defer_for
            if hook_context_conflict_session_id is not None:
                stored_task.hook_context_conflict_session_id = (
                    hook_context_conflict_session_id
                )
            if stamp_backoff:
                stored_task.spawn_error_count += 1
                delay = min(
                    _SPAWN_ERROR_BACKOFF_INITIAL_SECONDS
                    * (2 ** (stored_task.spawn_error_count - 1)),
                    _SPAWN_ERROR_BACKOFF_CAP_SECONDS,
                )
                stored_task.next_eligible_at = datetime.now(UTC) + timedelta(
                    seconds=delay
                )
        save_dev_queue(store)
    return reverted


def _park_running_task_blocked_on_user(
    *,
    ticket_id: str,
    client_name: str,
    disposition: str,
    breadcrumbs: str,
    expected_session_id: str | None = None,
    unproductive: bool = True,
    created_at: datetime | None = None,
    codex_orphan_session_id: str | None = None,
) -> None:
    """Move a still-RUNNING claimed task to BLOCKED_ON_USER, clearing session_id.

    ``unproductive`` is forwarded to :func:`transition_task_status` (#2114).
    The two pre-spawn callers (the dirty-worktree guard and the codex
    capability gate) pass ``False``: no session was ever spawned for the
    claim, so there is no RUNNING exit to charge -- and charging it ratchets
    a park that re-derives on every claim (a false ``dirty_worktree``, a
    codex-incapable host) toward ``attempt_cap_blocked``, burying the
    specific signal behind a generic one, exactly as
    ``_park_stale_dispatch_gate`` already reasons. The post-spawn caller
    (``cw.reconcile.codex_boot``, a session that really ran and was orphaned)
    keeps the default charge.

    Shared by every pre-spawn park path that needs to leave a task for operator
    inspection rather than silently retrying it (the dirty-worktree guard and
    the codex capability gate, #1238) — both need the identical lock, load,
    match-by-(ticket_id, client, status==RUNNING), transition, clear
    session_id, save shape; this is the single copy. ``ticket_id``/``client_name``
    are keyword-only (both plain ``str``, no type-system distinction between
    them) so a future edit can't silently transpose them at a call site.

    ``expected_session_id`` (optional) re-verifies ``stored_task.session_id ==
    expected_session_id`` under the *same* ``dev_queue_lock()`` acquisition
    that performs the transition — closing the check-then-use window a caller
    would otherwise have if it read ``session_id`` from an earlier, unlocked
    snapshot (``cw.reconcile.codex_boot``'s boot pass, #1727 round 5: the row
    can be re-claimed by a fresh session between that snapshot read and this
    call). A mismatch skips the park silently (the row no longer belongs to
    the session the caller thinks it does) rather than misfiling a healthy,
    unrelated session as parked. The two pre-spawn callers (dirty-worktree
    guard, codex capability gate) omit it: they run synchronously, same-tick,
    before any session_id has been stamped, so there is no snapshot to go
    stale.

    ``created_at`` (#2219) is the pre-spawn callers' identity instead: they
    pass their claimed task's ``created_at`` so a duplicate RUNNING row for
    the same ``(ticket_id, client)`` is never parked in their place. Both
    identities route through :func:`_find_running_row`; in every real call
    exactly one of them is supplied.

    Also emits SESSION_NEEDS_ATTENTION (#1257) using the canonical 9-field
    payload shape (see ``_route_scope_gated_approval`` in routing.py), reading
    ``stored_task.session_id``/``stored_task.lane`` before ``session_id`` is
    cleared below. ``breadcrumbs`` is caller-supplied human-readable detail
    (e.g. the codex-capability probe's ``.detail`` string, or a stale
    worktree's path) -- distinct from ``disposition``, which is the short
    reason code also stamped as the task's ``disposition``.

    ``codex_orphan_session_id`` (optional, #2307) is the session a
    ``cw.reconcile.codex_boot`` park leaves ACTIVE because a codex writer may
    still be alive in its worktree. It is stamped after the transition, which
    has just cleared the previous episode's link, so ``session_id`` being
    cleared below does not sever the row from that session:
    ``cw.reconcile.codex_reparks`` follows the link on reconcile ticks. It
    mirrors ``hook_context_conflict_session_id``'s conditional stamp in
    :func:`_revert_claimed_task_to_pending`; every other caller omits it.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        stored_task = _find_running_row(
            store,
            ticket_id,
            client_name,
            created_at=created_at,
            session_id=expected_session_id,
        )
        if stored_task is not None:
            transition_task_status(
                stored_task,
                QueueItemStatus.BLOCKED_ON_USER,
                disposition=disposition,
                unproductive=unproductive,
            )
            record_event(
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                {
                    "session_id": stored_task.session_id or "",
                    "session_name": "",
                    "client": client_name,
                    "ticket_id": ticket_id,
                    "claude_session_id": None,
                    "paused_status": disposition,
                    "breadcrumbs": breadcrumbs,
                    "crashed": False,
                    "lane": stored_task.lane,
                },
                correlation_id=ticket_id,
            )
            stored_task.session_id = None
            if codex_orphan_session_id is not None:
                stored_task.codex_orphan_session_id = codex_orphan_session_id
        save_dev_queue(store)


def _stamp_spawn_success(
    task: TicketTask,
    *,
    client_name: str,
    session_id: str,
    worktree_path: Path,
) -> None:
    """Persist the spawn-success state onto the stored RUNNING row.

    Stamps session_id so the completion consumer can match SESSION_COMPLETED
    events to the correct (current) session and reject stale events from prior
    crashed sessions for the same ticket (GitHub #97), clears the spawn-failure
    counters the successful spawn just invalidated, consumes the per-arrival
    regress markers, and records stage_base_ref.

    Extracted from :func:`_spawn_claimed_task` to keep that function inside the
    PLR statement budget, mirroring :func:`_codex_capability_gate`'s extraction
    for the same reason. Sole caller; it runs after the executor returns, so
    every write here is predicated on a spawn that genuinely succeeded.

    The stored row is re-found by the spawned task's ``created_at`` via
    :func:`_find_running_row` (#2219), so a duplicate RUNNING row for the same
    ``(ticket_id, client)`` is never stamped with this session in its place.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        stored_task = _find_running_row(
            store, task.ticket_id, client_name, created_at=task.created_at
        )
        if stored_task is not None:
            stored_task.session_id = session_id
            stored_task.spawn_error_count = 0
            stored_task.next_eligible_at = None
            # #1631: the single write site for the durable "a session was
            # genuinely spawned for this row" fact. Unconditional and
            # write-once-to-True -- reaching here IS the proof, and no
            # revert/requeue path ever clears it. reconcile's
            # timed-out-merged backstop reads it to tell a usage-limit-only
            # attempt history (which leaves spawn_error_count at 0, so the
            # counters alone cannot say) apart from a task that really ran
            # and shipped.
            stored_task.ever_spawned = True
            # #1674: the worktree just proved reusable, so any recorded
            # hook-context conflict is stale evidence — cleared here
            # atomically with the other spawn-failure counters.
            stored_task.hook_context_conflict_session_id = None
            # #1794: executor.spawn above already wrote the in-memory
            # task's regressed_into_stage into the new session's
            # queue_metadata, so the per-arrival marker is consumed.
            # Clear it so it never leaks into a later, unrelated stage
            # entry (the false positive the cumulative regress_attempts
            # counter would have produced).
            # #1801: this clear is unconditional and runs BEFORE any
            # reap could ever observe a no-sentinel death, which is
            # why a spawn that dies silently loses the signal for
            # good -- evaluated and accepted, see the field's comment
            # in src/cw/models/tasks.py for the full reasoning.
            stored_task.regressed_into_stage = None
            # #2337: the operator's plan_scope_drift grant is consumed the
            # same way, and just as unconditionally -- it was written into
            # this spawn's queue_metadata above and is meant for exactly the
            # IMPL session the approval requeued. Surviving past it would let
            # the grant cover a later round's drift it was never given for.
            stored_task.scope_drift_approved_extra_files = None
            stored_task.scope_drift_approved_head = None
            # #1730: stage-gated clear -- unlike regressed_into_stage
            # (cleared unconditionally at the next spawn), this marker
            # must survive an intervening non-REVIEW spawn (e.g. Rule
            # 5a's self-heal regresses to IMPL, not REVIEW) so it is
            # only consumed when a REVIEW-stage session is actually
            # about to read the delivered comments.
            if stored_task.stage == Stage.REVIEW:
                stored_task.pending_operator_comment = False
            # R5: stamp stage_base_ref -- non-fatal on failure
            try:
                head_sha = git_output(
                    ["-C", str(worktree_path), "rev-parse", "HEAD"], timeout=5
                )
                stored_task.stage_base_ref = head_sha.strip()
            except subprocess.SubprocessError as exc:
                _log.warning(
                    "dispatch: stage_base_ref failed for %s: %s",
                    task.ticket_id,
                    exc,
                )
        save_dev_queue(store)
