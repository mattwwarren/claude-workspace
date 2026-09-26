"""Worktree provisioning and session spawn for one claimed task.

The path a row takes once :func:`~cw.dispatch.claim.screening._claim_next_pending`
has claimed it RUNNING: the codex capability gate, worktree creation (with the
#2213 occupied-worktree deferral and the stale-tree liveness, dirty and removal
guards), the #1286 approved-plan bypass, the executor spawn, and the failure
routing back to PENDING. Extracted verbatim from the historical flat
``cw.dispatch.claim`` module by the package split (#2378).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from cw.dev_queue import (
    _impl_bypass_plan_available,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.lifecycle import _advance_stage
from cw.dispatch.claim.claimed_row import (
    _park_running_task_blocked_on_user,
    _revert_claimed_task_to_pending,
    _stamp_spawn_success,
)
from cw.dispatch.claim.codex_capability import _codex_capability_gate, _SpawnOutcome
from cw.dispatch.claim.events import _emit_worktree_occupied_skip_event
from cw.events import record_event
from cw.exceptions import (
    HookContextConflictError,
    StaleWorktreeError,
    UsageLimitError,
    WorktreeError,
    WorktreeOccupiedError,
)
from cw.executor import resolve_executor, resolve_pipeline_stages
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.worktree import (
    check_not_main_checkout,
    create_worktree,
    live_home_reason,
    remove_worktree,
    unsaved_work_reason,
    worktree_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree import UnresolvablePathWarningKey

_log = logging.getLogger("cw.dispatch")


# How long a claim that found its reused worktree occupied by a live session or
# daemon worker (``WorktreeOccupiedError``, #2213) is held off before the row is
# eligible again. Short: the occupant is typically the prior stage's worker
# still leaving the roster, and pickup after it goes should be prompt. Long
# enough that the SAME tick does not re-claim the row it just released and burn
# every remaining slot on one head-of-line ticket, starving the free tickets
# queued behind it. Fixed rather than exponential because nothing is failing:
# this is a wait, not a retry of a broken operation.
_OCCUPIED_DEFER_SECONDS: int = 30


def _apply_plan_bypass_if_available(
    task: TicketTask, client: ClientConfig, worktree_path: Path
) -> None:
    """Advance a PLAN-stage task straight to IMPL if a plan is already there.

    GitHub #1286: closes the gap where every automatic re-entry at
    ``Stage.PLAN`` re-ran a full Stage 1 planning pass from scratch even when
    a valid, signed-off ``.cw/plan.md`` was already sitting in the ticket's
    reused worktree. ``allow_tracker_fallback=False`` keeps this hot
    per-claim-path check network-free -- Stage 1 is about to run and post the
    plan anyway if the local check misses.

    Extracted from :func:`_spawn_claimed_task` to keep that function inside
    the PLR branch/statement budget, mirroring :func:`_codex_capability_gate`
    and :func:`_stamp_spawn_success`'s extractions for the same reason. Sole
    caller; ``task.stage`` must already be ``Stage.PLAN`` on entry.

    Mutates the STORED row under :func:`dev_queue_lock`, mirroring
    :func:`_stamp_spawn_success`'s load->find->mutate->save shape (with one
    addition: the find also matches on ``created_at`` so a duplicate RUNNING
    row for the same ticket and client is never advanced by mistake) plus
    :func:`~cw.dev_queue.requeue._apply_requeue_stage`'s
    old_stage/``_raise_stage_high_water``/``_emit_stage_change`` trio -- a
    bare in-memory ``task.stage = Stage.IMPL`` would never reach
    ``dev_queue.json``, so the next dispatch tick would re-read stage=PLAN
    and re-run this whole check from scratch. Only mirrored onto the
    caller-held ``task`` (so this tick's ``executor.spawn(stage=task.stage,
    ...)`` builds the IMPL prompt, not PLAN) once the stored row is found and
    persisted.

    Two guards run before the (file-I/O) plan check, cheapest first:

    - #1286 Fix B: ``task.regressed_into_stage == Stage.PLAN`` means an
      operator explicitly regressed this ticket back to PLAN (``cw dev-queue
      requeue --stage plan --regress``, via ``_stage_regress``). That call
      clears the approval markers but does NOT delete the worktree's stale,
      still-signed-off ``.cw/plan.md`` -- so without this guard the bypass
      would silently defeat the operator's explicit re-plan request.
    - #1286 Fix C: the pipeline resolved by
      :func:`~cw.executor.resolve_pipeline_stages` (lane override, then
      client default) may not contain ``Stage.IMPL`` at all (pipelines are
      user-configurable per client/lane). Attempting the advance anyway
      would raise ``ValueError`` out of ``_raise_stage_high_water``'s
      ``stages.index()`` call.
    """
    if task.regressed_into_stage == Stage.PLAN:
        _log.debug(
            "dispatch: %r's approved-plan auto-bypass skipped -- task was"
            " deliberately regressed into PLAN, honoring the operator's"
            " re-plan request over the (possibly stale) signed-off plan.md"
            " (#1286)",
            task.ticket_id,
        )
        return

    stages = resolve_pipeline_stages(task, client)
    if Stage.IMPL not in stages:
        _log.debug(
            "dispatch: %r's approved-plan auto-bypass skipped -- the"
            " resolved pipeline (lane=%s) has no IMPL stage (#1286)",
            task.ticket_id,
            task.lane,
        )
        return

    bypass = _impl_bypass_plan_available(task, client, allow_tracker_fallback=False)
    if not bypass.available:
        return

    stored_task = None
    with dev_queue_lock():
        store = load_dev_queue()
        for candidate in store.tasks:
            # created_at disambiguates duplicate RUNNING rows for one
            # (ticket_id, client) -- reachable via add-after-terminal plus
            # ``requeue --from-completed`` (see ``_find_ticket``'s own
            # newest-created_at tie-break). session_id is not stamped yet
            # (this runs before executor.spawn / _stamp_spawn_success), so it
            # cannot serve here; created_at is never reassigned.
            if (
                candidate.ticket_id == task.ticket_id
                and candidate.client == client.name
                and candidate.status == QueueItemStatus.RUNNING
                and candidate.created_at == task.created_at
            ):
                stored_task = candidate
                break
        if stored_task is not None:
            _advance_stage(stored_task, stages, Stage.IMPL)
            save_dev_queue(store)

    if stored_task is not None:
        # Mirror onto the in-memory task so this tick's
        # executor.spawn(stage=task.stage, ...) below builds the IMPL
        # prompt, not PLAN.
        task.stage = stored_task.stage
        _log.info(
            "dispatch: %r has an approved, signed-off plan already"
            " on disk (%s) -- bypassing Stage 1 and spawning at"
            " IMPL directly (#1286)",
            task.ticket_id,
            worktree_path / ".cw" / "plan.md",
        )
    else:
        # Race guard: _claim_next_pending persisted this row as RUNNING
        # (still at Stage.PLAN) just before this function ran, so it should
        # always be found here. If it is missing or no longer RUNNING
        # (reaped/requeued/removed between claim and spawn), do NOT advance
        # and do NOT spawn with a mutated stage -- leave task.stage untouched
        # so this tick spawns /auto-dev-plan as normal.
        _log.warning(
            "dispatch: %r's approved-plan auto-bypass found no"
            " matching RUNNING row in the dev queue (client=%s)"
            " -- leaving stage at PLAN (#1286)",
            task.ticket_id,
            client.name,
        )


def _defer_occupied_claim(
    task: TicketTask,
    client: ClientConfig,
    exc: WorktreeOccupiedError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Release a claim whose reused worktree is occupied; spawn nothing (#2213).

    ``create_worktree(refresh_on_reuse=True)`` raised
    :exc:`~cw.exceptions.WorktreeOccupiedError`: a live cw session, a live
    daemon-roster worker, or an indeterminate read of either (fail closed) may be
    operating in the ticket's per-ticket worktree. Spawning a second worker into
    it is the hazard the ticket exists to prevent, so nothing is spawned and the
    worktree is never removed -- though HEAD may already have moved if a
    fast-forward landed just before the occupant was found (#2233).

    The stale-worktree path (a wrong-branch tree that ``create_worktree`` refuses
    with ``StaleWorktreeError``) routes here too when an occupant is present: it
    consults liveness first and defers through this helper. It removes the tree
    only when the tree is BOTH unoccupied and clean; an occupied tree is left
    for a later tick, and a dirty one is parked for the operator instead.

    The row goes back to PENDING for a later tick as a RELEASE, not a failure
    (:func:`_revert_claimed_task_to_pending` with ``defer_for``): no attempt is
    charged and no spawn error is stamped. The returned outcome sets ``occupied``
    and NOT ``spawn_error``, so the lane circuit breaker never counts it -- a
    long-lived occupant would otherwise pause the whole lane over a per-ticket
    condition. The reason is recorded in the log, in ``_SpawnOutcome.error`` and
    on the operator's emit line, and a ``dispatch.tick`` event with
    ``skip_reason=worktree_occupied`` is emitted through the emitter the #2077
    pre-claim screen shares.
    """
    _log.warning(
        "dispatch_tick: worktree %s for %s/%s is occupied (%s); not spawning, "
        "returning the task to PENDING for a later tick",
        exc.path,
        client.name,
        task.ticket_id,
        exc.reason,
    )
    _emit_worktree_occupied_skip_event(client.name, task.ticket_id)
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        defer_for=timedelta(seconds=_OCCUPIED_DEFER_SECONDS),
        created_at=task.created_at,
    )
    if emit is not None:
        emit(
            f"OCCUPIED {client.name}/{task.ticket_id} worktree={exc.path}"
            f" ({exc.reason}); deferred, not spawned"
        )
    return _SpawnOutcome(occupied=True, error=exc.reason)


def _defer_genuinely_live_hook_conflict(
    task: TicketTask,
    client: ClientConfig,
    exc: HookContextConflictError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Release a claim whose hook context is held by a genuinely live
    session (#2077) -- sibling of _defer_occupied_claim for the
    DAEMON-conflict branch of HookContextConflictError.

    The residual race the pre-claim occupancy screen cannot close: the
    screen's precompute found the worktree free, but by the time the
    executor wrote its hook context a live session was homed there. Same
    no-charge, no-breaker release as _defer_occupied_claim.

    Deliberately does NOT stamp hook_context_conflict_session_id (unlike
    the non-live branch in _spawn_claimed_task): that field's only consumer
    (reconcile/concierge.py recipe 1) reads it as "only an operator
    closing the session clears this" -- wrong semantics for a condition
    that resolves itself. Recipe 1 only evaluates BLOCKED_ON_USER rows;
    this revert leaves the row PENDING, so the stamp would only matter if a
    LATER, unrelated park inherited it -- which omitting it here prevents.
    """
    _log.warning(
        "dispatch_tick: hook context for %s/%s is held by genuinely live"
        " session %s; not spawning, returning the task to PENDING for a"
        " later tick",
        client.name,
        task.ticket_id,
        exc.conflicting_session_id,
    )
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        defer_for=timedelta(seconds=_OCCUPIED_DEFER_SECONDS),
        created_at=task.created_at,
    )
    _emit_worktree_occupied_skip_event(client.name, task.ticket_id)
    if emit is not None:
        emit(
            f"OCCUPIED {client.name}/{task.ticket_id} hook_context held"
            f" by live session={exc.conflicting_session_id}; deferred,"
            " not spawned"
        )
    return _SpawnOutcome(occupied=True, error=str(exc))


def _handle_hook_context_conflict(
    task: TicketTask,
    client: ClientConfig,
    exc: HookContextConflictError,
    *,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Route a ``HookContextConflictError`` from the spawn path.

    Must be called from inside the ``except`` clause (``_log.exception``
    below relies on the active exception). Extracted from
    :func:`_spawn_claimed_task` to keep it inside its PLR0911 return budget
    once the #2077 genuinely-live branch was added.

    A genuinely live occupant (#2077) is a self-resolving wait, not a spawn
    failure: :func:`_defer_genuinely_live_hook_conflict` releases it without
    a charge or a breaker increment. Otherwise behaviour is deliberately
    identical to the broad spawn-failure path (revert to PENDING with the
    #868 backoff); the ONLY addition is recording WHICH session's live
    cw-context.json blocked the worktree, so concierge recipe 1 can stop
    requeuing a row that cannot spawn until that session is closed (GitHub
    #1674). The refusal itself lives there, not here.
    """
    if exc.genuinely_live:
        return _defer_genuinely_live_hook_conflict(task, client, exc, emit=emit)
    _log.exception(
        "dispatch_tick: hook-context conflict for %s/%s"
        " (blocking session=%s); reverting task to PENDING",
        client.name,
        task.ticket_id,
        exc.conflicting_session_id,
    )
    _revert_claimed_task_to_pending(
        client.name,
        task.ticket_id,
        stamp_backoff=True,
        hook_context_conflict_session_id=exc.conflicting_session_id,
        created_at=task.created_at,
    )
    return _SpawnOutcome(spawn_error=True, error=str(exc))


def _raise_if_stale_tree_occupied(
    client: ClientConfig,
    branch: str,
    *,
    daemon: NativeDaemonClient,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> None:
    """Raise :exc:`WorktreeOccupiedError` if the stale tree must not be removed.

    Guard 1 of the stale-worktree handler in :func:`_spawn_claimed_task`
    (#2213): consults :func:`cw.worktree.live_home_reason` -- the same predicate
    the same-branch reuse refresh uses, failing closed -- on the branch's
    canonical worktree path. *daemon* is the caller's own resolved
    :class:`~cw.native_daemon.NativeDaemonClient` (#2213 round 7), passed
    straight through rather than letting ``live_home_reason`` default to the
    real client -- a test injecting :class:`~cw.native_daemon.FakeNativeDaemonClient`
    into this claim path must be the thing consulted, not the host's real
    roster. A live cw session or daemon-roster worker homed there, or an
    unreadable state or roster, means the tree is not ours to remove. The
    raised error is caught by ``_spawn_claimed_task``'s
    ``except WorktreeOccupiedError`` and reaches :func:`_defer_occupied_claim`.
    Returns normally (no occupant) so the dirty check and removal may follow.
    *warned_unresolvable* is forwarded to :func:`~cw.worktree.live_home_reason`
    unchanged (#2240) -- the dispatch loop threads a process-lifetime set
    through here so a poisoned session/worker record does not re-warn every
    tick.
    """
    stale_tree = worktree_path_for(client, branch)
    occupant = live_home_reason(
        stale_tree, daemon=daemon, warned_unresolvable=warned_unresolvable
    )
    if occupant is None:
        return
    msg = (
        f"Refusing to remove stale worktree at {stale_tree} for branch "
        f"{branch!r}: another worker may be operating in it ({occupant}). "
        "The worktree was not touched."
    )
    raise WorktreeOccupiedError(msg, path=stale_tree, reason=occupant)


def _spawn_claimed_task(
    task: TicketTask,
    client: ClientConfig,
    *,
    resolved_native_daemon: NativeDaemonClient,
    parent: str | None,
    emit: Callable[[str], None] | None,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> _SpawnOutcome:
    """Spawn a Claude session for one already-claimed (RUNNING) task.

    Creates the worktree, spawns the session, stamps session_id +
    stage_base_ref, and emits SESSION_SPAWNED. On :class:`UsageLimitError` or
    any other spawn failure, reverts the task to PENDING and returns an outcome
    flagging the caller to break out of the slot/lane loops.
    """
    try:
        # Codex capability gate (#1238): a codex-backed stage that cannot reach
        # a usable `codex` CLI parks BLOCKED_ON_USER before any real per-task
        # work runs (worktree creation included) — extracted to a helper to
        # keep this function within the PLR branch/statement budget; no-op for
        # non-codex backends. Runs first so a codex-incapable host never pays
        # for worktree provisioning on a task that's about to be parked.
        parked = _codex_capability_gate(task, client)
        if parked is not None:
            return parked

        # Provision the worktree on the feature branch the auto-dev
        # skills push to (`<feature_branch_prefix>/<id>`, e.g.
        # dev/662) so cw and the worker agree on one branch — no
        # mid-pipeline rename that would trip the reuse guard (#712).
        # The session NAME still uses AUTO_DEV_LABEL_PREFIX (set in
        # the executor), which reconcile parses for the ticket id.
        branch = f"{client.feature_branch_prefix}/{task.ticket_id}"
        # Create a real git worktree (idempotent — returns existing
        # path if already created). Replaces a previous bug where
        # dispatch made an empty directory and relied on
        # ``claude -w`` to turn it into a worktree, which never
        # worked because that flag takes a name rather than a path.
        try:
            # allow_dirty_reuse: staged stages reuse one per-ticket
            # worktree and legitimately leave cross-stage churn (#712).
            # refresh_on_reuse (#2213): a reused per-ticket worktree can sit
            # behind origin/<branch>, so ask for a best-effort refresh. NOTE
            # this does a network `git fetch` (can be slow) and fast-forwards
            # only an unoccupied (no live cw session or daemon-roster worker),
            # clean, strictly-behind worktree. The refresh has two outcomes,
            # handled oppositely -- OCCUPIED is not "did not move it": a
            # fast-forward (and, #2233, its post-ff submodule sync) can land
            # for real before an occupant is found:
            #   * NOT refreshed (dirty, diverged, failed fetch, branch absent):
            #     the tree is ours, just not up to date. create_worktree
            #     returns the path and we spawn on it; the reason is logged
            #     (cw.worktree) -- there is no friction-notes surface here.
            #   * OCCUPIED (a live session or worker may be using the tree, or
            #     that cannot be ruled out): create_worktree RAISES
            #     WorktreeOccupiedError. HEAD may already have moved (a
            #     fast-forward, and #2233's post-ff submodule sync, can land
            #     before the occupancy re-check finds the occupant) -- that
            #     move is never undone. The raise must never fall through to
            #     a spawn, so it is handled by its own narrow ``except``
            #     below (not the StaleWorktreeError branch, which removes a
            #     tree: an occupied one is never removed). A stale
            #     (wrong-branch) tree gets its own occupancy check inside
            #     that branch and reaches the same deferral.
            # ticket_id: names the ticket on the worktree.fast_forwarded audit
            # event a refresh that moves HEAD records; no other effect.
            worktree_path = create_worktree(
                client,
                branch,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                ticket_id=task.ticket_id,
                native_daemon=resolved_native_daemon,
            )
        except StaleWorktreeError:
            # A stale worktree (wrong branch / not a worktree) refused
            # reuse (#404). No session exists yet, so reconcile's
            # TIMED_OUT cleanup will never fire for it — without
            # removing it here the task reverts to PENDING and re-hits
            # the same stale tree every tick (an infinite spin). Force-
            # remove it (best-effort) so the next claim rebuilds fresh,
            # then re-raise into the handler below to revert to PENDING.
            # Caught narrowly as StaleWorktreeError (not WorktreeError)
            # so the main-checkout guard never triggers a removal.
            #
            # Three guards stand between a stale tree and that removal, in
            # this order:
            #
            # 1. Liveness (#2213): if a live cw session or daemon-roster
            #    worker is homed on the tree, or that cannot be ruled out
            #    (unreadable state or roster: fail closed), leave it alone
            #    and defer the claim through _defer_occupied_claim -- the
            #    same OCCUPIED_BY_LIVE_SESSION handling the reuse-refresh
            #    refusal reaches. A wrong branch does not mean an idle tree:
            #    a worker may be running in it. It comes BEFORE the dirty
            #    check because it is the stronger reason to keep hands off
            #    (removing a live worker's tree destroys its working state),
            #    and because unsaved_work_reason shells out to git inside a
            #    directory that worker may be mutating concurrently, so the
            #    liveness answer must not depend on that read.
            # 2. Dirty-check guard (#425): if the stale tree contains
            #    unsaved work, skip the removal and park the task as
            #    BLOCKED_ON_USER instead of PENDING so the operator can
            #    inspect. The outer except handler will not overwrite
            #    BLOCKED_ON_USER (it checks status == RUNNING before
            #    reverting).
            # 3. Only a tree that is both unoccupied and clean is removed
            #    (the #404 spin fix above).
            #
            # Guard 1 raises WorktreeOccupiedError from inside this handler;
            # the sibling ``except WorktreeOccupiedError`` below (an exception
            # raised here propagates to the enclosing try, so it does catch
            # it) hands it to _defer_occupied_claim. Raising rather than
            # returning that helper's outcome inline keeps this function within
            # the PLR0911 return budget and gives the reuse-refresh refusal and
            # this one a single exit.
            _raise_if_stale_tree_occupied(
                client,
                branch,
                daemon=resolved_native_daemon,
                warned_unresolvable=warned_unresolvable,
            )
            unsaved = unsaved_work_reason(client, branch)
            if unsaved is not None:
                _log.warning(
                    "dispatch: stale worktree %s/%s has unsaved work (%s)"
                    " — leaving for operator inspection; parking as"
                    " BLOCKED_ON_USER",
                    client.name,
                    branch,
                    unsaved,
                )
                # #2114: the breadcrumb names WHICH predicate fired and the
                # base ref it measured against -- `dirty_worktree` on a
                # visibly clean tree is otherwise undiagnosable.
                _park_running_task_blocked_on_user(
                    ticket_id=task.ticket_id,
                    client_name=client.name,
                    disposition="dirty_worktree",
                    breadcrumbs=f"{worktree_path_for(client, branch)}: {unsaved}",
                    unproductive=False,
                    created_at=task.created_at,
                )
            else:
                with contextlib.suppress(WorktreeError, OSError):
                    remove_worktree(client, branch, force=True)
            raise

        # Guard against the #300 regression: if create_worktree
        # returns the main checkout path (degenerate path-computation
        # or symlink indirection), refuse the spawn.  create_worktree
        # normally catches this itself, but a mocked or buggy
        # implementation could still return the same path.
        check_not_main_checkout(worktree_path, client)

        if task.stage == Stage.PLAN:
            _apply_plan_bypass_if_available(task, client, worktree_path)

        # Function-level import breaks the gating<->claim import cycle:
        # cw.dispatch.gating imports this module at top level, so claim.py
        # must defer its own reach back into gating (mirrors the #698
        # reconcile/_shared -> cw.dispatch precedent the ticket cites).
        from cw.dispatch.gating import _invalidate_stale_context_json

        _invalidate_stale_context_json(task, client, worktree_path)

        executor = resolve_executor(task, client, native_daemon=resolved_native_daemon)
        # wall_clock_budget_seconds is always None since the process-kill-
        # timeout removal: no executor is handed a kill deadline. The codex
        # backend treats None as unlimited (no proc.kill on a timer), and the
        # value written into cw-context.json is informational only.
        session_id = executor.spawn(
            stage=task.stage,
            task=task,
            worktree=worktree_path,
            client=client,
            parent=parent,
            wall_clock_budget_seconds=None,
        )

        _stamp_spawn_success(
            task,
            client_name=client.name,
            session_id=session_id,
            worktree_path=worktree_path,
        )

        record_event(
            OrchestratorEventType.SESSION_SPAWNED,
            {
                "ticket_id": task.ticket_id,
                "client": client.name,
                "session_id": session_id,
                "lane": task.lane,
            },
        )

        if emit is not None:
            emit(
                f"SPAWN {client.name}/{task.ticket_id}"
                f" session={session_id}"
                f" worktree={worktree_path}"
            )
    except UsageLimitError as exc:
        # Narrow catch for fleet-wide usage limits. Raised by
        # executor.spawn → NativeDaemonClient.spawn_bg when the
        # claude output matches USAGE_LIMIT_RE. The task was claimed
        # to RUNNING but no session_id was assigned (spawn failed);
        # revert it explicitly to PENDING below, then break so no
        # further slots are tried this tick.
        #
        # The raw spawn-time message is NOT logged here: native_daemon
        # ._usage_limit_error already logs it exactly once per raise (#1409),
        # and repeating it would give two records for one event.
        _log.warning(
            "dispatch_tick: usage limit detected for %s/%s; setting back-off"
            " (reset_at=%s)",
            client.name,
            task.ticket_id,
            exc.reset_at,
        )
        # Revert the claimed task back to PENDING — spawn never succeeded.
        _revert_claimed_task_to_pending(
            client.name, task.ticket_id, created_at=task.created_at
        )
        return _SpawnOutcome(
            usage_limit_detected=True, usage_limit_reset_at=exc.reset_at
        )
    except HookContextConflictError as exc:
        # Narrow catch ahead of the broad handler below (order matters —
        # HookContextConflictError is a plain CwError subclass and would
        # otherwise fall through). See _handle_hook_context_conflict.
        return _handle_hook_context_conflict(task, client, exc, emit=emit)
    except WorktreeOccupiedError as exc:
        # Narrow catch ahead of the broad handler (order matters -- this is a
        # WorktreeError and would otherwise be reverted WITH a spawn-error
        # backoff and trip the circuit breaker). See _defer_occupied_claim.
        return _defer_occupied_claim(task, client, exc, emit=emit)
    except Exception as exc:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331.
        # Paired tests: TestDispatchTickSpawnErrors in
        # tests/test_dispatch.py:1097+ (asserts the loop survives
        # spawn failures and the task is reverted to PENDING).
        #
        # Catch broad like the reconcile guard above: a backend
        # outage (tmux pane exhaustion, transient daemon failure,
        # OSError from the adapter) must NOT kill the loop. The
        # task was just claimed RUNNING by _claim_next_pending; it
        # would otherwise be left in a half-state (status=RUNNING,
        # session_id=None) requiring manual repair. Revert to
        # PENDING + clear session_id so the next tick (or
        # reconcile) can retry. Break to skip this client's
        # remaining slots this tick — re-trying the same failing
        # backend immediately would just spin. See GitHub issue
        # #149.
        _log.exception(
            "dispatch_tick: spawn failed for %s/%s; reverting task to PENDING",
            client.name,
            task.ticket_id,
        )
        _revert_claimed_task_to_pending(
            client.name,
            task.ticket_id,
            stamp_backoff=True,
            created_at=task.created_at,
        )
        return _SpawnOutcome(spawn_error=True, error=str(exc))

    return _SpawnOutcome(spawned=True)
