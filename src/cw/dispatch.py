"""Tick-based dispatch loop: claim pending TicketTasks and spawn Claude sessions."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pydantic
import yaml

from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    AutoDevResult,
    parse_stdout,
)
from cw.config import (
    load_clients,
    load_effective_clients,
    load_effective_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue, load_plan, save_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import (
    MissingWorkspaceError,
    StaleWorktreeError,
    UsageLimitError,
    WorktreeError,
)
from cw.executor import ClaudeNativeExecutor
from cw.models import (
    ClientConfig,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    reconcile,
    resolve_headless_budget,
    ticket_id_for_session,
)
from cw.worktree import (
    check_main_ff_safety,
    check_not_main_checkout,
    create_worktree,
    fast_forward_main,
    get_head_branch,
    is_main_behind_origin,
    remove_worktree,
    worktree_has_unsaved_work,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        LaneConfig,
        OrchestratorConfig,
        OrchestratorEvent,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient

_DISPATCH_CONSUMER = "dispatch"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchTickResult:
    """Return value of :func:`dispatch_tick`.

    ``spawned`` — number of sessions started this tick.
    ``usage_limit_detected`` — True if a usage limit was detected this tick
    (either from spawn-time :class:`~cw.exceptions.UsageLimitError` or from
    :attr:`~cw.reconcile.ReconcileReport.usage_limited`). The caller
    (:func:`run_dispatch_loop`) uses this to set the back-off window.
    ``--once`` mode intentionally does not back off (single tick, no loop state).
    """

    spawned: int
    usage_limit_detected: bool = False


def _claim_next_pending(
    client_name: str,
    *,
    lane: str,
    priority_ticket_ids: list[str] | None = None,
) -> TicketTask | None:
    """Atomically claim the next PENDING task for a client in a specific lane.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* in *lane* as RUNNING, saves, and returns it.
    Returns None if no pending task exists for the client in the lane.

    If *priority_ticket_ids* is provided, prefer claiming PENDING tasks in
    that order (only those whose ticket_id appears in the list).  Tasks not
    referenced by the list are skipped at this stage; they will be claimed
    by subsequent ticks once the prioritised tasks are exhausted (the
    parameter is intentionally a *preference*, not a filter — see the
    fallback after the priority loop).
    """
    with dev_queue_lock():
        store = load_dev_queue()
        if priority_ticket_ids:
            for ticket_id in priority_ticket_ids:
                for task in store.tasks:
                    if (
                        task.client == client_name
                        and task.ticket_id == ticket_id
                        and task.lane == lane
                        and task.status == QueueItemStatus.PENDING
                    ):
                        task.status = QueueItemStatus.RUNNING
                        task.attempts += 1
                        save_dev_queue(store)
                        return task
        pending = sorted(
            [
                t
                for t in store.tasks
                if t.client == client_name
                and t.lane == lane
                and t.status == QueueItemStatus.PENDING
            ],
            key=lambda t: (-t.priority, t.created_at),
        )
        if pending:
            task = pending[0]
            task.status = QueueItemStatus.RUNNING
            task.attempts += 1
            save_dev_queue(store)
            return task
    return None


def _lane_stats_for_client(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, dict[str, int]]:
    """Per-lane ``{claimed, running, blocked, pending}`` counts for event payloads.

    Why task-based running: RUNNING/BLOCKED_ON_USER tasks carry ``lane``;
    sessions carry ``lane`` as of #594, but occupancy counting stays task-join
    based per ADR-0006 / Phase 4a scope (stamped-but-not-read by the
    scheduler). BLOCKED_ON_USER occupies its lane slot per ADR-0006, so
    ``running + blocked`` is the total occupied count. ``blocked`` is split out
    so operators can see at a glance why claimed=0 when pending>0 (#588).
    """
    stats: dict[str, dict[str, int]] = {}
    for lane_cfg in client.effective_lanes:
        running = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.RUNNING
        )
        blocked = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.BLOCKED_ON_USER
        )
        pending = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        stats[lane_cfg.name] = {
            "claimed": 0,
            "running": running,
            "blocked": blocked,
            "pending": pending,
        }
    return stats


@dataclass(frozen=True)
class _SpawnOutcome:
    """Result of attempting to spawn one claimed task.

    ``spawned`` — True if a session was started (counters should be bumped).
    ``usage_limit_detected`` — True if a :class:`UsageLimitError` fired.
    ``spawn_error`` — True if a broad spawn failure reverted the task.

    Both error flags signal the caller to break out of the slot/lane loops.
    """

    spawned: bool = False
    usage_limit_detected: bool = False
    spawn_error: bool = False


def _revert_claimed_task_to_pending(client_name: str, ticket_id: str) -> None:
    """Revert a still-RUNNING claimed task back to PENDING, clearing session_id.

    Used by both the usage-limit and broad spawn-error paths: the task was
    claimed to RUNNING by :func:`_claim_next_pending` but spawn never
    succeeded, so it must return to PENDING for a later tick to retry.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        for stored_task in store.tasks:
            if (
                stored_task.ticket_id == ticket_id
                and stored_task.client == client_name
                and stored_task.status == QueueItemStatus.RUNNING
            ):
                stored_task.status = QueueItemStatus.PENDING
                stored_task.session_id = None
                break
        save_dev_queue(store)


def _emit_usage_limit_skip_events(
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    state: CwState,
) -> None:
    """Emit dispatch.tick(skip_reason=USAGE_LIMITED) for every client.

    Called when the usage-limit back-off window is still active: no client is
    dispatched this tick; each gets a skip event with ``claimed=0`` and a
    per-lane breakdown.
    """
    for client in clients.values():
        running_count = sum(
            1
            for s in state.sessions
            if s.client == client.name
            and s.origin == SessionOrigin.DAEMON
            and s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        )
        cap = config.per_client_ceiling.get(client.name, config.default_ceiling)
        with dev_queue_lock():
            queue_snapshot = load_dev_queue()
        pending_count = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name and t.status == QueueItemStatus.PENDING
        )
        # Per-lane breakdown for the event payload (claimed=0 for all).
        backoff_lane_stats = _lane_stats_for_client(client, queue_snapshot)
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": client.name,
                "claimed": 0,
                "pending": pending_count,
                "running": running_count,
                "cap": cap,
                "skip_reason": DispatchSkipReason.USAGE_LIMITED,
                "lanes": backoff_lane_stats,
            },
        )


FRESHNESS_NON_MAIN_HEAD = "non_main_head"
FRESHNESS_MAIN_BEHIND = "main_behind_origin"


def _resolve_freshness(
    client: ClientConfig,
    *,
    auto_ff: bool,
    warned_fetch_fail: set[str] | None,
) -> tuple[bool, str | None]:
    """Run the freshness gate for a client, returning (stale, freshness_detail).

    Checks whether the client's local default branch is behind origin.  When
    ``auto_ff`` is set and the branch is safely behind, attempts a
    fast-forward and clears the stale flag on success.  On any freshness-check
    error, logs and treats the client as fresh so a transient network issue
    never blocks the whole loop.

    Returns ``(False, None)`` when fresh (or successfully fast-forwarded).
    Returns ``(True, "non_main_head")`` when the dispatch repo's HEAD is on a
    non-default branch — ``fast_forward_main`` is skipped entirely to avoid a
    spurious WorktreeError.  Returns ``(True, "main_behind_origin")`` for all
    other stale conditions.
    """
    try:
        stale, local_sha, origin_sha, behind_count = is_main_behind_origin(
            client, warned_fetch_fail=warned_fetch_fail
        )
    except Exception:  # noqa: BLE001
        # Defense-in-depth: _fetch_default_branch now handles
        # FileNotFoundError/PermissionError internally; this catches
        # other unexpected OS errors (e.g., git not on PATH, network
        # issues raising RuntimeError from the adapter).
        _log.warning(
            "dispatch_tick: freshness check failed for %s; proceeding",
            client.name,
        )
        return (False, None)

    if stale:
        # Guard: detect non-default HEAD before attempting auto-ff.
        # When HEAD != default_branch, fast_forward_main would raise WorktreeError
        # and log a confusing message. Bail early with a distinct detail key so
        # the operator WARN can surface the specific remedy.
        head_branch = get_head_branch(client)
        if head_branch is not None and head_branch != client.default_branch:
            return (True, FRESHNESS_NON_MAIN_HEAD)

    if stale and auto_ff:
        ff_safety = check_main_ff_safety(client)
        if ff_safety == "behind":
            try:
                fast_forward_main(client, ignore_untracked=True)
                # Why: double-fetch accepted — is_main_behind_origin fetches
                # and git pull --ff-only fetches again. Acceptable for a
                # single-user tool.
                _log.info(
                    "auto-ff: %s/main: %s..%s (%d commits)",
                    client.name,
                    local_sha[:8],
                    origin_sha[:8],
                    behind_count,
                )
                stale = False
            except (WorktreeError, MissingWorkspaceError) as exc:
                _log.warning(
                    "auto-ff: fast-forward failed for %s: %s",
                    client.name,
                    exc,
                )
            # Why: no git-level lock — concurrent dispatch loops are safe;
            # git pull --ff-only is idempotent when already current.
    return (stale, FRESHNESS_MAIN_BEHIND if stale else None)


def _emit_stale_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_stale: set[tuple[str, str]] | None,
    freshness_detail: str | None = None,
) -> None:
    """Emit TICKET_NEEDS_SYNC + dispatch.tick for a freshness-gated client.

    Records one TICKET_NEEDS_SYNC per pending task (de-duplicating the
    operator WARN via ``warned_stale``), then a single dispatch.tick with
    ``skip_reason=FRESHNESS_GATE`` and ``freshness_detail`` set to the
    provided value (``"non_main_head"`` or ``"main_behind_origin"``).
    """
    stale_tasks = [
        {"ticket_id": t.ticket_id, "client": client.name, "lane": t.lane}
        for t in queue_snapshot.tasks
        if t.client == client.name and t.status == QueueItemStatus.PENDING
    ]
    # Fetch branch name once for the non-main-head WARN (not per ticket).
    non_main_branch: str | None = None
    if freshness_detail == FRESHNESS_NON_MAIN_HEAD:
        non_main_branch = get_head_branch(client)
    for payload in stale_tasks:
        record_event(OrchestratorEventType.TICKET_NEEDS_SYNC, payload)
        if emit is not None:
            ticket_key = (client.name, payload["ticket_id"])
            if warned_stale is None or ticket_key not in warned_stale:
                if freshness_detail == FRESHNESS_NON_MAIN_HEAD:
                    branch_str = non_main_branch or "(detached)"
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        f" repo HEAD is on '{branch_str}',"
                        f" expected '{client.default_branch}'"
                        f" — run: git -C {client.workspace_path}"
                        f" checkout {client.default_branch}"
                    )
                else:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main behind origin, ticket skipped"
                    )
                if warned_stale is not None:
                    warned_stale.add(ticket_key)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.FRESHNESS_GATE,
            "freshness_detail": freshness_detail,
            "lanes": _lane_stats_for_client(client, queue_snapshot),
        },
    )


def _spawn_claimed_task(
    task: TicketTask,
    client: ClientConfig,
    *,
    config: OrchestratorConfig,
    resolved_native_daemon: NativeDaemonClient,
    parent: str | None,
    emit: Callable[[str], None] | None,
) -> _SpawnOutcome:
    """Spawn a Claude session for one already-claimed (RUNNING) task.

    Creates the worktree, spawns the session, stamps session_id +
    stage_base_ref, and emits SESSION_SPAWNED. On :class:`UsageLimitError` or
    any other spawn failure, reverts the task to PENDING and returns an outcome
    flagging the caller to break out of the slot/lane loops.
    """
    try:
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
            worktree_path = create_worktree(client, branch, allow_dirty_reuse=True)
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
            # Dirty-check guard (#425): if the stale tree contains
            # unsaved work, skip the removal and park the task as
            # BLOCKED_ON_USER instead of PENDING so the operator can
            # inspect. The outer except handler will not overwrite
            # BLOCKED_ON_USER (it checks status == RUNNING before
            # reverting).
            if worktree_has_unsaved_work(client, branch):
                _log.warning(
                    "dispatch: stale worktree %s/%s has unsaved work"
                    " — leaving for operator inspection; parking as"
                    " BLOCKED_ON_USER",
                    client.name,
                    branch,
                )
                with dev_queue_lock():
                    store = load_dev_queue()
                    for stored_task in store.tasks:
                        if (
                            stored_task.ticket_id == task.ticket_id
                            and stored_task.client == client.name
                            and stored_task.status == QueueItemStatus.RUNNING
                        ):
                            stored_task.status = QueueItemStatus.BLOCKED_ON_USER
                            stored_task.session_id = None
                            break
                    save_dev_queue(store)
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

        executor = ClaudeNativeExecutor(native_daemon=resolved_native_daemon)
        session_id = executor.spawn(
            stage=task.stage,
            task=task,
            worktree=worktree_path,
            client=client,
            parent=parent,
            wall_clock_budget_seconds=resolve_headless_budget(task, None, config),
        )

        # Stamp session_id on the queued task so the completion
        # consumer can match SESSION_COMPLETED events to the
        # correct (current) session and reject stale events from
        # prior crashed sessions for the same ticket. See GitHub
        # issue #97.
        with dev_queue_lock():
            store = load_dev_queue()
            for stored_task in store.tasks:
                if (
                    stored_task.ticket_id == task.ticket_id
                    and stored_task.client == client.name
                    and stored_task.status == QueueItemStatus.RUNNING
                ):
                    stored_task.session_id = session_id
                    # R5: stamp stage_base_ref -- non-fatal on failure
                    try:
                        head_sha = subprocess.check_output(
                            [
                                "git",
                                "-C",
                                str(worktree_path),
                                "rev-parse",
                                "HEAD",
                            ],
                            text=True,
                            timeout=5,
                        )
                        stored_task.stage_base_ref = head_sha.strip()
                    except subprocess.SubprocessError as exc:
                        _log.warning(
                            "dispatch: stage_base_ref failed for %s: %s",
                            task.ticket_id,
                            exc,
                        )
                    break
            save_dev_queue(store)

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
    except UsageLimitError:
        # Narrow catch for fleet-wide usage limits. Raised by
        # executor.spawn → NativeDaemonClient.spawn_bg when the
        # claude output matches USAGE_LIMIT_RE. The task was claimed
        # to RUNNING but no session_id was assigned (spawn failed);
        # revert it explicitly to PENDING below, then break so no
        # further slots are tried this tick.
        _log.warning(
            "dispatch_tick: usage limit detected for %s/%s; setting back-off",
            client.name,
            task.ticket_id,
        )
        # Revert the claimed task back to PENDING — spawn never succeeded.
        _revert_claimed_task_to_pending(client.name, task.ticket_id)
        return _SpawnOutcome(usage_limit_detected=True)
    except Exception:  # noqa: BLE001
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
        _revert_claimed_task_to_pending(client.name, task.ticket_id)
        return _SpawnOutcome(spawn_error=True)

    return _SpawnOutcome(spawned=True)


@dataclass(frozen=True)
class _ClientDispatchResult:
    """Aggregate outcome of dispatching one client's lanes for a tick.

    ``spawned`` — sessions started for the client this tick.
    ``usage_limit_detected`` — True if any lane hit a usage limit.
    """

    spawned: int = 0
    usage_limit_detected: bool = False


def _dispatch_client_lanes(
    client: ClientConfig,
    effective_lanes: list[LaneConfig],
    queue_snapshot: DevQueueStore,
    *,
    running_count: int,
    client_ceiling: int,
    cap: int,
    pending_count: int,
    cap_full: bool,
    running_by_lane: dict[str, int],
    priority_ids: list[str] | None,
    config: OrchestratorConfig,
    resolved_native_daemon: NativeDaemonClient,
    parent: str | None,
    emit: Callable[[str], None] | None,
) -> _ClientDispatchResult:
    """Claim + spawn across a client's lanes, then emit its dispatch.tick.

    Walks each effective lane within the Tier-1 client slot budget, claiming
    and spawning one task per granted slot.  Breaks out of the lane walk on
    the first usage-limit or spawn-error.  Always records the per-client
    dispatch.tick event (with the resolved skip_reason and per-lane stats).
    """
    client_spawned = 0
    spawn_error = False
    usage_limit_detected = False
    # True when any lane has pending>0 but grant<=0 due to occupied slots
    # (RUNNING + BLOCKED_ON_USER >= max_parallel). Distinguishes the
    # previously misleading skip_reason=no_pending case (#588).
    lane_cap_blocked = False

    # Tier-1 client slot budget: use the session-based running_count (not
    # the task-based total_running) so pre-existing DAEMON sessions without
    # a corresponding task still occupy slots (backward compat). The per-
    # lane running_by_lane counts govern per-lane grants within this budget.
    available_client_slots = client_ceiling - running_count
    lane_stats: dict[str, dict[str, int]] = {}

    for lane_cfg in effective_lanes:
        if available_client_slots <= 0:
            break
        if lane_cfg.paused:
            continue
        # running_in_lane = RUNNING + BLOCKED_ON_USER (total occupied slots).
        running_in_lane = running_by_lane.get(lane_cfg.name, 0)
        # blocked_in_lane = BLOCKED_ON_USER only (for per-lane breakdown).
        blocked_in_lane = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.BLOCKED_ON_USER
        )
        pending_in_lane = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        grant = min(
            lane_cfg.max_parallel - running_in_lane,
            pending_in_lane,
            available_client_slots,
        )
        # Detect: pending work exists but the lane cap is full of occupied
        # slots (RUNNING + BLOCKED_ON_USER >= max_parallel). Raises the
        # skip_reason to LANE_CAP_BLOCKED instead of the misleading NO_PENDING.
        if grant <= 0 and pending_in_lane > 0:
            lane_cap_blocked = True
        lane_claimed = 0
        for _ in range(max(0, grant)):
            task: TicketTask | None = _claim_next_pending(
                client.name,
                lane=lane_cfg.name,
                priority_ticket_ids=priority_ids,
            )
            if task is None:
                break

            outcome = _spawn_claimed_task(
                task,
                client,
                config=config,
                resolved_native_daemon=resolved_native_daemon,
                parent=parent,
                emit=emit,
            )
            if outcome.usage_limit_detected:
                usage_limit_detected = True
            if outcome.spawn_error:
                spawn_error = True
            if outcome.spawned:
                running_count += 1
                client_spawned += 1
                lane_claimed += 1
                available_client_slots -= 1

            if usage_limit_detected or spawn_error:
                break

        lane_stats[lane_cfg.name] = {
            "claimed": lane_claimed,
            "running": running_in_lane - blocked_in_lane,
            "blocked": blocked_in_lane,
            "pending": pending_in_lane,
        }

        if usage_limit_detected or spawn_error:
            break

    if emit is not None:
        emit(
            f"{client.name}: spawned={client_spawned}"
            f" cap_full={int(cap_full)}"
            f" lane_cap_blocked={int(lane_cap_blocked)}"
        )

    skip_reason = _resolve_dispatch_skip_reason(
        usage_limit_detected=usage_limit_detected,
        cap_full=cap_full,
        spawn_error=spawn_error,
        lane_cap_blocked=lane_cap_blocked,
        client_spawned=client_spawned,
    )

    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": client_spawned,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": skip_reason,
            "lanes": lane_stats,
        },
    )
    return _ClientDispatchResult(
        spawned=client_spawned, usage_limit_detected=usage_limit_detected
    )


def _resolve_dispatch_skip_reason(
    *,
    usage_limit_detected: bool,
    cap_full: bool,
    spawn_error: bool,
    lane_cap_blocked: bool,
    client_spawned: int,
) -> DispatchSkipReason:
    """Resolve the dispatch.tick skip_reason via first-match precedence.

    Mirrors the operator resolution order (issue #459, #588):
    1. freshness_gate — handled by early-continue before this is called
    2. usage_limited — usage limit detected this tick for this client
    3. cap_full — running_count >= cap before loop entered
    4. lane_cap_blocked — pending>0 but every lane slot is occupied by
       RUNNING or BLOCKED_ON_USER tasks; grant<=0 for all lanes
    5. spawn_error — exception broke the loop (regardless of client_spawned)
    6. no_pending — loop exited with zero claims and no spawn error
    7. none — at least one session spawned
    """
    if usage_limit_detected:
        return DispatchSkipReason.USAGE_LIMITED
    if cap_full:
        return DispatchSkipReason.CAP_FULL
    if lane_cap_blocked:
        return DispatchSkipReason.LANE_CAP_BLOCKED
    if spawn_error:
        return DispatchSkipReason.SPAWN_ERROR
    if client_spawned == 0:
        return DispatchSkipReason.NO_PENDING
    return DispatchSkipReason.NONE


def _reconcile_usage_limited() -> bool:
    """Run the best-effort reconcile preamble, returning its usage-limit flag.

    Returns True if reconcile reported a usage limit; False on success without
    a limit or when reconcile raised (logged and swallowed so a transient
    failure never kills the tick — phantoms are reaped next tick).
    """
    reconcile_report = None
    try:
        reconcile_report = reconcile()
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331 (4-part justification):
        # 1. reconcile() calls ``claude agents --json`` and native-daemon roster
        #    I/O — failure modes include subprocess crash and JSON decode errors.
        # 2. Logging: _log.exception captures the full traceback with exc_info.
        # 3. Non-critical: reconcile is best-effort housekeeping. Skipping a tick
        #    just means phantoms get reaped on the next dispatch_tick.
        # 4. Paired test: tests/test_dispatch.py
        #    test_reconcile_failure_does_not_crash_dispatch_tick.
        _log.exception("reconcile failed during dispatch_tick; continuing")
    return reconcile_report is not None and reconcile_report.usage_limited


def _build_plan_order(*, use_plan: bool) -> dict[str, list[str]]:
    """Build the per-client ticket priority ordering from the persisted plan.

    Returns an empty mapping when ``use_plan`` is False or no plan is found;
    otherwise maps each client to its plan-ordered ticket ids.
    """
    plan_order_by_client: dict[str, list[str]] = {}
    if use_plan:
        plan = load_plan()
        if plan is not None:
            for plan_task in plan.tasks:
                plan_order_by_client.setdefault(plan_task.client, []).append(
                    plan_task.ticket_id,
                )
    return plan_order_by_client


def dispatch_tick(
    config: OrchestratorConfig,
    *,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    warned_stale: set[tuple[str, str]] | None = None,
    warned_fetch_fail: set[str] | None = None,
    usage_limited_until: datetime | None = None,
    auto_ff: bool = True,
    client_filter: str | None = None,
) -> DispatchTickResult:
    """Run one dispatch tick.

    For each client that has pending TicketTasks, check how many DAEMON
    sessions are currently ACTIVE or IDLE and compare against the
    per-client cap from *config*.  If below the cap, claim one pending
    task and spawn a Claude session for it.

    Args:
        config: Orchestrator config (per-client caps, tick interval).
        use_plan: If True, respect the persisted DispatchPlan ordering.
        parent: Optional parent session ID. When set, every spawned
            worker is linked to it (``parent_session_id`` +
            bidirectional ``worker_session_ids``) so ``cw orchestrate
            workers`` can list dispatched workers as first-class
            sessions.
        emit: Optional callable for operator-facing stdout lines.
            When None, all human-readable output is suppressed (quiet
            mode).  Typically ``click.echo`` in CLI context.
        warned_stale: Mutable set of ``(client, ticket_id)`` pairs that
            have already received a "main behind origin" warning during
            this dispatcher run.  Prevents repeated spam across ticks.
            Caller owns the set; mutated in-place.
        warned_fetch_fail: Mutable set of client names that have already
            received a fetch-failure WARNING during this dispatcher run.
            Suppresses repeated WARNINGs for persistently unreachable
            remotes.  Caller owns the set; mutated in-place.
        usage_limited_until: When set and in the future, all clients are
            skipped with ``skip_reason=USAGE_LIMITED`` and the function
            returns immediately. The back-off window is set by the
            caller (:func:`run_dispatch_loop`) when a
            :class:`~cw.exceptions.UsageLimitError` is detected.
            Single-tick (``--once``) mode does not set back-off.
        auto_ff: When True (default), attempt to fast-forward local main
            automatically before emitting TICKET_NEEDS_SYNC. Only fires
            when ``check_main_ff_safety`` returns ``"behind"``; other
            states (``"ahead"``, ``"diverged"``, ``"detached"``) still
            fall through to the stale-block path. Pass ``False`` to
            restore legacy block-only behavior.
        client_filter: When set, narrow the client loop to this single
            client name. The caller is responsible for validating that the
            name exists before calling; an unknown name silently produces
            an empty tick.

    Returns:
        :class:`DispatchTickResult` with ``spawned`` count and
        ``usage_limit_detected`` flag.
    """
    resolved_native_daemon = native_daemon or get_native_daemon_client()
    any_usage_limit_detected = _reconcile_usage_limited()
    clients = load_effective_clients()
    if client_filter is not None:
        clients = {client_filter: clients[client_filter]}
    state = load_state()
    spawned = 0

    plan_order_by_client = _build_plan_order(use_plan=use_plan)

    # Usage-limit back-off gate: if the window is still active, skip all clients
    # this tick and emit a dispatch.tick event with skip_reason=USAGE_LIMITED.
    if usage_limited_until is not None and datetime.now(UTC) < usage_limited_until:
        _emit_usage_limit_skip_events(clients, config, state)
        return DispatchTickResult(spawned=0, usage_limit_detected=False)

    # Tier-1: optionally cap how many clients are eligible per tick.
    # max_parallel_clients=None preserves the original behaviour (all clients).
    dispatched_client_count = 0
    for client in clients.values():
        if (
            config.max_parallel_clients is not None
            and dispatched_client_count >= config.max_parallel_clients
        ):
            break
        # --- Freshness gate ---
        # Check whether the client's local default branch is behind origin
        # before claiming any ticket.  Stale repos cause sessions to exit
        # immediately with local_main_diverged_from_origin, burning a slot.
        # On any error, log and proceed so a transient network issue never
        # blocks the whole loop.  When auto_ff and safely behind, this also
        # fast-forwards local main and clears the stale flag on success.
        stale, freshness_detail = _resolve_freshness(
            client, auto_ff=auto_ff, warned_fetch_fail=warned_fetch_fail
        )

        # Count running daemon sessions and cap — hoisted above freshness gate
        # so all four numeric fields (claimed, pending, running, cap) are
        # available when emitting dispatch.tick with skip_reason=freshness_gate.
        running_count = sum(
            1
            for s in state.sessions
            if s.client == client.name
            and s.origin == SessionOrigin.DAEMON
            and s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        )

        # Tier-2 ceiling: per-client slot budget across all lanes.
        client_ceiling = config.per_client_ceiling.get(
            client.name, config.default_ceiling
        )
        # Keep legacy cap alias for freshness-gate and back-off event payloads.
        cap = client_ceiling

        # Pre-claim pending count for dispatch.tick payload. Single lock
        # acquisition — reused for stale_tasks when stale=True (avoids a
        # second load on the freshness-gate path).
        with dev_queue_lock():
            queue_snapshot = load_dev_queue()
        pending_count = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name and t.status == QueueItemStatus.PENDING
        )

        if stale:
            _emit_stale_skip(
                client,
                queue_snapshot,
                pending_count=pending_count,
                running_count=running_count,
                cap=cap,
                emit=emit,
                warned_stale=warned_stale,
                freshness_detail=freshness_detail,
            )
            continue

        # Why: incremented only past the freshness gate — a stale/skipped
        # client does not consume Tier-1 quota, so max_parallel_clients=N
        # always grants N *dispatchable* clients per tick.
        dispatched_client_count += 1
        priority_ids = plan_order_by_client.get(client.name)
        cap_full = running_count >= client_ceiling

        # Build per-lane running count from tasks→sessions join.
        # Tasks in RUNNING or BLOCKED_ON_USER with an active session_id count
        # toward their lane's cap (ADR-0006: BLOCKED_ON_USER occupies the slot).
        # Reuses the queue_snapshot taken above — nothing between the two
        # points mutates the queue (auto-ff is git-only).
        running_by_lane: dict[str, int] = {}
        for qt in queue_snapshot.tasks:
            if qt.client != client.name:
                continue
            if qt.status not in (
                QueueItemStatus.RUNNING,
                QueueItemStatus.BLOCKED_ON_USER,
            ):
                continue
            lane_key = qt.lane
            running_by_lane[lane_key] = running_by_lane.get(lane_key, 0) + 1

        # Resolve effective lanes. For clients with no declared lanes, use the
        # synthesized default lane but override its max_parallel with the client
        # ceiling so backward-compat behaviour is preserved.
        effective_lanes = client.effective_lanes
        if not client.lanes:
            effective_lanes = [
                effective_lanes[0].model_copy(update={"max_parallel": client_ceiling})
            ]

        client_result = _dispatch_client_lanes(
            client,
            effective_lanes,
            queue_snapshot,
            running_count=running_count,
            client_ceiling=client_ceiling,
            cap=cap,
            pending_count=pending_count,
            cap_full=cap_full,
            running_by_lane=running_by_lane,
            priority_ids=priority_ids,
            config=config,
            resolved_native_daemon=resolved_native_daemon,
            parent=parent,
            emit=emit,
        )
        spawned += client_result.spawned
        if client_result.usage_limit_detected:
            any_usage_limit_detected = True

    return DispatchTickResult(
        spawned=spawned, usage_limit_detected=any_usage_limit_detected
    )


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost_usd to task.total_cost_usd, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_stop — normal headless path)
      2. session.last_result.get('cost_usd') (populated by persist_last_result —
         event-replay path where signal_stop did not run)

    When both sources are absent, total_cost_usd is left unchanged.
    Called inside dev_queue_lock so the mutation is covered by the same
    save_dev_queue call that persists the COMPLETED status.
    """
    if session_id is None:
        return
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None:
        return
    cost: float | None = session.cost_usd
    if cost is None and isinstance(session.last_result, dict):
        raw_cost = session.last_result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            cost = float(raw_cost)
    if cost is not None:
        task.total_cost_usd = (task.total_cost_usd or 0.0) + cost


def _resolve_scope_tier(
    last_result: dict[str, object] | None, task: TicketTask
) -> str | None:
    """Resolve the effective scope tier for a scope-gated advance decision.

    Precedence mirrors reconcile's tier-unavailable fallback (#314, #696):
      1. ``last_result.scope.tier`` -- the plan sentinel's own classification.
      2. ``task.scope_hint`` -- operator/queue hint, used when the sentinel
         omits the tier.

    Why: a real PLAN-stage sentinel can legitimately carry ``scope.tier=null``
    (``lines_actual`` is unknown pre-impl), so a raw read blocked small tickets
    that should flow PLAN->IMPL unattended (#663 dogfood). Returns ``None`` when
    neither source supplies a tier -- the caller then blocks conservatively.
    """
    scope_val = last_result.get("scope") if last_result is not None else None
    tier = scope_val.get("tier") if isinstance(scope_val, dict) else None
    if isinstance(tier, str):
        return tier
    return task.scope_hint


def _stage_advance(task: TicketTask, clients: dict[str, ClientConfig]) -> None:
    """Advance task to next pipeline stage, or mark COMPLETED at terminal stage.

    Precondition: task.status must be RUNNING. Only called from the B2
    decision table which guards on RUNNING before dispatching to this helper.
    """
    if task.status != QueueItemStatus.RUNNING:
        msg = f"_stage_advance: expected RUNNING, got {task.status!r}"
        raise AssertionError(msg)
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        _log.warning(
            "dispatch: advance: client %r not found for task %r -- parking as BLOCKED",
            task.client,
            task.ticket_id,
        )
        task.status = QueueItemStatus.BLOCKED_ON_USER
        return
    pipeline = client_cfg.pipeline
    stages = pipeline.stages
    if task.stage not in stages:
        _log.warning(
            "dispatch: advance: stage %r not in pipeline for task %r",
            task.stage,
            task.ticket_id,
        )
        task.status = QueueItemStatus.BLOCKED_ON_USER
        return
    if task.stage == stages[-1]:
        task.status = QueueItemStatus.COMPLETED
    else:
        idx = stages.index(task.stage)
        task.stage = stages[idx + 1]
        task.status = QueueItemStatus.PENDING
        task.session_id = None  # R6: clear session_id on advance
        task.stage_base_ref = None  # cleared so next spawn stamps fresh ref


def apply_staged_decision(
    task: TicketTask,
    status: str | None,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> None:
    """Apply the B2 staged advance decision to a RUNNING task.

    The single advance authority shared by the consume path
    (_apply_events_to_store) and reconcile's emitted-sentinel router
    (_apply_sentinel_to_task), so staged dispatch advances regardless of which
    path observes the completion first (#698). Precondition: task.status is
    RUNNING. Mutates task in place.
    """
    if status in SCOPE_GATED_APPROVAL_STATUSES:
        # Rule 1: scope-gated approval; small tier auto-advances, large blocks.
        # Must fire before Rule 2 (SCOPE_GATED ⊂ PAUSED_FOR_USER_INPUT).
        # Tier resolves from the sentinel's scope.tier, falling back to
        # task.scope_hint when the sentinel omits it (#696).
        tier = _resolve_scope_tier(last_result, task)
        if tier == SCOPE_TIER_SMALL:
            _stage_advance(task, clients)
        else:
            task.status = QueueItemStatus.BLOCKED_ON_USER
    elif status in PAUSED_FOR_USER_INPUT_STATUSES:
        # Rule 2: pure pause (v4 statuses: ambiguities_pending_resolution,
        # premises_pending_verification). Scope-gated statuses caught by Rule 1.
        task.status = QueueItemStatus.BLOCKED_ON_USER
    elif status in STAGE_SUCCESS_STATUSES:
        # Rule 3: shipped -- advance or complete
        _stage_advance(task, clients)
    elif status == "no_op":
        # Rule 4: pre-flight already satisfied -- terminal
        # regardless of remaining stages
        task.status = QueueItemStatus.COMPLETED
    elif status in STAGE_FAILURE_STATUSES:
        # Rule 5: blocked/merge_gate_blocked/scope_exceeded/forbidden_area
        task.status = QueueItemStatus.BLOCKED_ON_USER
    else:
        # Rule 6: None/not dict/missing status -- conservative fallback
        # Why: unparseable sentinel must never silently advance/complete
        # (B2 correctness requirement). Changes pre-B2 behavior which
        # fell through to COMPLETED.
        task.status = QueueItemStatus.BLOCKED_ON_USER


def _apply_events_to_store(
    store: DevQueueStore,
    events: list[OrchestratorEvent],
    clients: dict[str, ClientConfig],
) -> int:
    """Apply SESSION_COMPLETED / SESSION_COMPLETED_INFERRED events to a store.

    Caller must hold ``dev_queue_lock``. Saves the store when tasks were
    transitioned; does NOT advance the event cursor — cursor advancement
    is the caller's responsibility after the lock is released.

    Returns the number of tasks transitioned to COMPLETED.
    """
    completed = 0
    for event in events:
        # Crashed events are emitted by reconcile only. For DAEMON
        # sessions reconcile has already reverted the task
        # RUNNING → PENDING; marking the task COMPLETED here would
        # shadow that revert and (worse) match the next freshly-
        # respawned RUNNING task for the same ticket_id, falsely
        # retiring a still-running session. For non-DAEMON crashed
        # sessions reconcile does not touch the queue, so a blanket
        # skip is conservative-safe (no queue task is expected to
        # match anyway). See GitHub issue #97.
        if event.payload.get("crashed"):
            continue
        ticket_id = event.payload.get("ticket_id")
        if not ticket_id:
            # Fallback: recover ticket_id from the session_name for events
            # produced before the reconciler emitted ticket_id explicitly.
            # Drains historical RUNNING tasks whose completion events
            # predate the producer-side fix.
            session_name = event.payload.get("session_name")
            if isinstance(session_name, str):
                ticket_id = ticket_id_for_session(session_name)
        if not ticket_id:
            continue
        event_session_id = event.payload.get("session_id")
        for task in store.tasks:
            if task.ticket_id != ticket_id:
                continue
            # Why: reconcile may have already set this task to BLOCKED_ON_USER
            # (salvaged paused-status session). The task is no longer RUNNING,
            # so this event is harmlessly skipped — overwriting with COMPLETED
            # would shadow BLOCKED_ON_USER, which downstream operators need.
            if task.status != QueueItemStatus.RUNNING:
                continue
            # Disambiguate stale events: when the event carries a
            # session_id and the task has been stamped with one, they
            # must agree. Either side missing a session_id falls back to
            # ticket_id-only matching for backward compatibility with
            # legacy tasks/events that predate the field.
            if (
                isinstance(event_session_id, str)
                and task.session_id is not None
                and task.session_id != event_session_id
            ):
                continue
            # SESSION_COMPLETED_INFERRED: world-state inferred completion (#315).
            # The signal_stop code already set task→COMPLETED directly, so this
            # path is a belt-and-suspenders fallback for tasks still RUNNING.
            # Bypass apply_staged_decision — no sentinel status to route.
            if event.payload.get("completion_source") == "world_state_inference":
                task.status = QueueItemStatus.COMPLETED
                task.session_id = None
                completed += 1
                break
            state = load_state()
            session = next(
                (s for s in state.sessions if s.id == event_session_id),
                None,
            )
            last_result = (
                session.last_result
                if session is not None and isinstance(session.last_result, dict)
                else None
            )
            status = last_result.get("status") if last_result is not None else None

            apply_staged_decision(task, status, last_result, clients)
            sid = event_session_id if isinstance(event_session_id, str) else None
            _accumulate_task_cost(task, sid)
            completed += 1
            break
    if completed:
        save_dev_queue(store)
    return completed


def consume_completed_sessions() -> int:
    """Process session.completed / session.completed_inferred events.

    Reads new SESSION_COMPLETED and SESSION_COMPLETED_INFERRED events from
    the inbox since the last cursor position for the "dispatch" consumer.
    For each event that carries a ``ticket_id`` in its payload, the
    corresponding TicketTask (if found in RUNNING state) is marked COMPLETED.
    SESSION_COMPLETED_INFERRED events bypass apply_staged_decision — the task
    already shipped; there is no sentinel status to route (#315).

    Advances the cursor after processing.

    Returns:
        Number of TicketTasks transitioned to COMPLETED.
    """
    events = read_events(
        consumer=_DISPATCH_CONSUMER,
        event_types=[
            OrchestratorEventType.SESSION_COMPLETED,
            OrchestratorEventType.SESSION_COMPLETED_INFERRED,
        ],
    )
    if not events:
        return 0

    # Persist sentinel-block summaries on Sessions BEFORE the advance
    # decision, so _apply_events_to_store reads each just-completed session's
    # last_result (status + scope.tier) instead of a stale/None value. Without
    # this ordering, a freshly-completed stage has last_result=None at decision
    # time → status=None → Rule 6 → BLOCKED_ON_USER, so the staged pipeline
    # never advances (#694). Producer side (worker stdout capture) is gated on
    # the orchestrator P1.A wiring; this consumer is forward-compatible with
    # events that lack a ``stdout`` payload (such an event leaves last_result
    # unset → the conservative-safe BLOCKED_ON_USER default).
    for event in events:
        session_id = event.payload.get("session_id")
        stdout = event.payload.get("stdout")
        if isinstance(session_id, str) and isinstance(stdout, str):
            persist_last_result(session_id, stdout)

    with dev_queue_lock():
        store = load_dev_queue()
        clients = load_effective_clients()
        completed = _apply_events_to_store(store, events, clients=clients)
        # Advance cursor inside the dev-queue lock so the cursor never
        # moves past events whose queue mutations haven't been persisted yet.
        advance_cursor(_DISPATCH_CONSUMER, events[-1].id)

    return completed


def persist_last_result(session_id: str, stdout: str) -> bool:
    """Parse *stdout* and write the result onto the matching Session.

    Returns True if a session was updated, False if no match or if parsing
    yielded nothing actionable. Never raises — parser failures surface as
    a synthetic blocker dict on ``Session.last_result`` so post-hoc
    inspection still has something to look at.
    """
    parsed = parse_stdout(stdout)
    with sessions_lock():
        state = load_state()
        target = None
        for session in state.sessions:
            if session.id == session_id:
                target = session
                break
        if target is None:
            _log.warning(
                "persist_last_result: session %s not found in state",
                session_id,
            )
            return False
        if isinstance(parsed, AutoDevResult):
            target.last_result = parsed.model_dump(mode="json")
        else:
            target.last_result = parsed.model_dump(mode="json")
        save_state(state)
    return True


def run_dispatch_loop(
    *,
    max_parallel: int | None = None,
    once: bool = False,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    auto_ff: bool = True,
    client: str | None = None,
) -> None:
    """Run the dispatch loop, optionally overriding per-client concurrency caps.

    Args:
        max_parallel: If set, override all per-client caps with this value.
        once: If True, run a single tick and return immediately.
        use_plan: If True, load the persisted DispatchPlan and use its
            ordering to claim tasks.  Falls back to enqueue order when no
            plan is found (load_plan returns None).
        parent: Optional orchestrator session ID. Threaded into each
            dispatch tick so spawned workers are linked back to the
            caller's session.
        native_daemon: Optional NativeDaemonClient for testing. Defaults
            to ``get_native_daemon_client()`` at call time. Used for
            spawning dispatched workers.
        emit: Optional callable for operator-facing stdout lines.
            When None, all human-readable output is suppressed (quiet
            mode for cron/scripted use).  Typically ``click.echo`` in
            CLI context.
        auto_ff: Passed through to each ``dispatch_tick`` call.  When
            True (default), stale-but-behind repos are fast-forwarded
            automatically. Pass False to restore legacy block-only
            behavior (``--no-auto-ff`` CLI flag).
        client: When set, scope each tick to this single client's queue.
            Validated at the CLI boundary before this function is called.
    """
    config = load_effective_config()

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()
    # Track fetch-fail-warn deduplication for persistently unreachable remotes.
    warned_fetch_fail: set[str] = set()
    # Back-off window: set when a UsageLimitError is detected during a tick.
    # Subsequent ticks skip all spawns until this window elapses.
    usage_limited_until: datetime | None = None

    while True:
        try:
            config = load_effective_config()
            if max_parallel is not None:
                clients = load_clients()
                overridden = dict.fromkeys(clients, max_parallel)
                config = config.model_copy(update={"per_client_ceiling": overridden})
        except (yaml.YAMLError, pydantic.ValidationError):
            _log.warning("dispatch: config reload failed; using last-good config")

        consume_completed_sessions()
        result = dispatch_tick(
            config,
            use_plan=use_plan,
            parent=parent,
            native_daemon=resolved_native_daemon,
            emit=emit,
            warned_stale=warned_stale,
            warned_fetch_fail=warned_fetch_fail,
            usage_limited_until=usage_limited_until,
            auto_ff=auto_ff,
            client_filter=client,
        )

        if result.usage_limit_detected and not once:
            usage_limited_until = datetime.now(UTC) + timedelta(
                seconds=config.usage_limit_backoff_seconds
            )
            _log.warning(
                "dispatch: usage limit detected; backing off until %s",
                usage_limited_until,
            )

        if once:
            return

        time.sleep(config.tick_interval_seconds)
