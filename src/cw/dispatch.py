"""Tick-based dispatch loop: claim pending TicketTasks and spawn Claude sessions."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pydantic
import yaml

from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    AutoDevResult,
    parse_stdout,
)
from cw.config import (
    load_clients,
    load_orchestrator_config,
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
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    AUTO_DEV_LABEL_PREFIX,
    reconcile,
    resolve_headless_budget,
    ticket_id_for_session,
)
from cw.spawn import spawn_create_impl
from cw.worktree import (
    check_main_ff_safety,
    check_not_main_checkout,
    create_worktree,
    fast_forward_main,
    is_main_behind_origin,
    remove_worktree,
    worktree_has_unsaved_work,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
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
    """Per-lane ``{claimed: 0, running, pending}`` counts for event payloads.

    Why task-based running: RUNNING/BLOCKED_ON_USER tasks carry ``lane``;
    sessions do not (``Session.lane`` is #560). BLOCKED_ON_USER occupies its
    lane slot per ADR-0006, so it counts as running here.
    """
    stats: dict[str, dict[str, int]] = {}
    for lane_cfg in client.effective_lanes:
        running = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status in (QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER)
        )
        pending = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        stats[lane_cfg.name] = {"claimed": 0, "running": running, "pending": pending}
    return stats


def dispatch_tick(
    config: OrchestratorConfig,
    *,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    warned_stale: set[tuple[str, str]] | None = None,
    usage_limited_until: datetime | None = None,
    auto_ff: bool = True,
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

    Returns:
        :class:`DispatchTickResult` with ``spawned`` count and
        ``usage_limit_detected`` flag.
    """
    resolved_native_daemon = native_daemon or get_native_daemon_client()
    any_usage_limit_detected = False
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
    if reconcile_report is not None and reconcile_report.usage_limited:
        any_usage_limit_detected = True
    clients = load_clients()
    state = load_state()
    spawned = 0

    plan_order_by_client: dict[str, list[str]] = {}
    if use_plan:
        plan = load_plan()
        if plan is not None:
            for plan_task in plan.tasks:
                plan_order_by_client.setdefault(plan_task.client, []).append(
                    plan_task.ticket_id,
                )

    # Usage-limit back-off gate: if the window is still active, skip all clients
    # this tick and emit a dispatch.tick event with skip_reason=USAGE_LIMITED.
    if usage_limited_until is not None and datetime.now(UTC) < usage_limited_until:
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
        # blocks the whole loop.
        try:
            stale, local_sha, origin_sha, behind_count = is_main_behind_origin(client)
        except Exception:  # noqa: BLE001
            # Defense-in-depth: _fetch_default_branch now handles
            # FileNotFoundError/PermissionError internally; this catches
            # other unexpected OS errors (e.g., git not on PATH, network
            # issues raising RuntimeError from the adapter).
            _log.warning(
                "dispatch_tick: freshness check failed for %s; proceeding",
                client.name,
            )
            stale = False

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

        if stale:
            stale_tasks = [
                {"ticket_id": t.ticket_id, "client": client.name}
                for t in queue_snapshot.tasks
                if t.client == client.name and t.status == QueueItemStatus.PENDING
            ]
            for payload in stale_tasks:
                record_event(OrchestratorEventType.TICKET_NEEDS_SYNC, payload)
                if emit is not None:
                    ticket_key = (client.name, payload["ticket_id"])
                    if warned_stale is None or ticket_key not in warned_stale:
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
                    "lanes": _lane_stats_for_client(client, queue_snapshot),
                },
            )
            continue

        # Why: incremented only past the freshness gate — a stale/skipped
        # client does not consume Tier-1 quota, so max_parallel_clients=N
        # always grants N *dispatchable* clients per tick.
        dispatched_client_count += 1
        priority_ids = plan_order_by_client.get(client.name)
        client_spawned = 0
        cap_full = running_count >= client_ceiling
        spawn_error = False
        usage_limit_detected = False

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
            running_in_lane = running_by_lane.get(lane_cfg.name, 0)
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
            lane_claimed = 0
            for _ in range(max(0, grant)):
                task: TicketTask | None = _claim_next_pending(
                    client.name,
                    lane=lane_cfg.name,
                    priority_ticket_ids=priority_ids,
                )
                if task is None:
                    break

                try:
                    branch = f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}"
                    # Create a real git worktree (idempotent — returns existing
                    # path if already created). Replaces a previous bug where
                    # dispatch made an empty directory and relied on
                    # ``claude -w`` to turn it into a worktree, which never
                    # worked because that flag takes a name rather than a path.
                    try:
                        worktree_path = create_worktree(client, branch)
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
                                        and stored_task.status
                                        == QueueItemStatus.RUNNING
                                    ):
                                        stored_task.status = (
                                            QueueItemStatus.BLOCKED_ON_USER
                                        )
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

                    label = branch
                    session_id = spawn_create_impl(
                        client=client,
                        worktree=worktree_path,
                        prompt=f"/auto-dev {task.ticket_id} --headless",
                        label=label,
                        native_daemon=resolved_native_daemon,
                        parent=parent,
                        ticket_id=task.ticket_id,
                        headless=True,
                        task=task,
                        wall_clock_budget_seconds=resolve_headless_budget(
                            task, None, config
                        ),
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

                    running_count += 1
                    spawned += 1
                    client_spawned += 1
                    lane_claimed += 1
                    available_client_slots -= 1
                except UsageLimitError:
                    # Narrow catch for fleet-wide usage limits. Raised by
                    # spawn_create_impl → NativeDaemonClient.spawn_bg when the
                    # claude output matches USAGE_LIMIT_RE. The task was claimed
                    # to RUNNING but no session_id was assigned (spawn failed);
                    # revert it explicitly to PENDING below, then break so no
                    # further slots are tried this tick.
                    usage_limit_detected = True
                    any_usage_limit_detected = True
                    _log.warning(
                        "dispatch_tick: usage limit detected for"
                        " %s/%s; setting back-off",
                        client.name,
                        task.ticket_id,
                    )
                    # Revert the claimed task back to PENDING — spawn never succeeded.
                    with dev_queue_lock():
                        store = load_dev_queue()
                        for stored_task in store.tasks:
                            if (
                                stored_task.ticket_id == task.ticket_id
                                and stored_task.client == client.name
                                and stored_task.status == QueueItemStatus.RUNNING
                            ):
                                stored_task.status = QueueItemStatus.PENDING
                                stored_task.session_id = None
                                break
                        save_dev_queue(store)
                    break  # do not retry other slots this tick
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
                    spawn_error = True
                    _log.exception(
                        "dispatch_tick: spawn failed for %s/%s;"
                        " reverting task to PENDING",
                        client.name,
                        task.ticket_id,
                    )
                    with dev_queue_lock():
                        store = load_dev_queue()
                        for stored_task in store.tasks:
                            if (
                                stored_task.ticket_id == task.ticket_id
                                and stored_task.client == client.name
                                and stored_task.status == QueueItemStatus.RUNNING
                            ):
                                stored_task.status = QueueItemStatus.PENDING
                                stored_task.session_id = None
                                break
                        save_dev_queue(store)
                    break

                if usage_limit_detected or spawn_error:
                    break

            lane_stats[lane_cfg.name] = {
                "claimed": lane_claimed,
                "running": running_in_lane,
                "pending": pending_in_lane,
            }

            if usage_limit_detected or spawn_error:
                break

        if emit is not None:
            emit(f"{client.name}: spawned={client_spawned} cap_full={int(cap_full)}")

        # skip_reason: first-match precedence (see operator resolution, issue #459)
        # 1. freshness_gate — handled by early-continue above
        # 2. usage_limited — usage limit detected this tick for this client
        # 3. cap_full — running_count >= cap before loop entered
        # 4. spawn_error — exception broke the loop (regardless of client_spawned)
        # 5. no_pending — loop exited with zero claims and no spawn error
        # 6. none — at least one session spawned
        if usage_limit_detected:
            skip_reason = DispatchSkipReason.USAGE_LIMITED
        elif cap_full:
            skip_reason = DispatchSkipReason.CAP_FULL
        elif spawn_error:
            skip_reason = DispatchSkipReason.SPAWN_ERROR
        elif client_spawned == 0:
            skip_reason = DispatchSkipReason.NO_PENDING
        else:
            skip_reason = DispatchSkipReason.NONE

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


def _apply_events_to_store(
    store: DevQueueStore,
    events: list[OrchestratorEvent],
) -> int:
    """Apply SESSION_COMPLETED events to an already-loaded DevQueueStore.

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
            state = load_state()
            session = next(
                (s for s in state.sessions if s.id == event_session_id),
                None,
            )
            if (
                session is not None
                and isinstance(session.last_result, dict)
                and session.last_result.get("status") in PAUSED_FOR_USER_INPUT_STATUSES
            ):
                task.status = QueueItemStatus.BLOCKED_ON_USER
            else:
                task.status = QueueItemStatus.COMPLETED
            sid = event_session_id if isinstance(event_session_id, str) else None
            _accumulate_task_cost(task, sid)
            completed += 1
            break
    if completed:
        save_dev_queue(store)
    return completed


def consume_completed_sessions() -> int:
    """Process session.completed events and mark tasks COMPLETED in the queue.

    Reads new SESSION_COMPLETED events from the inbox since the last
    cursor position for the "dispatch" consumer.  For each event that
    carries a ``ticket_id`` in its payload, the corresponding TicketTask
    (if found in RUNNING state) is marked COMPLETED.

    Advances the cursor after processing.

    Returns:
        Number of TicketTasks transitioned to COMPLETED.
    """
    events = read_events(
        consumer=_DISPATCH_CONSUMER,
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    if not events:
        return 0

    with dev_queue_lock():
        store = load_dev_queue()
        completed = _apply_events_to_store(store, events)
        # Advance cursor inside the dev-queue lock so the cursor never
        # moves past events whose queue mutations haven't been persisted yet.
        advance_cursor(_DISPATCH_CONSUMER, events[-1].id)

    # Persist sentinel-block summaries on Sessions whose completion event
    # carried captured stdout. Producer side (worker stdout capture) is
    # gated on the orchestrator P1.A wiring; this consumer is forward-
    # compatible with events that lack a ``stdout`` payload.
    for event in events:
        session_id = event.payload.get("session_id")
        stdout = event.payload.get("stdout")
        if isinstance(session_id, str) and isinstance(stdout, str):
            persist_last_result(session_id, stdout)

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
    """
    config = load_orchestrator_config()

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()
    # Back-off window: set when a UsageLimitError is detected during a tick.
    # Subsequent ticks skip all spawns until this window elapses.
    usage_limited_until: datetime | None = None

    while True:
        try:
            config = load_orchestrator_config()
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
            usage_limited_until=usage_limited_until,
            auto_ff=auto_ff,
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
