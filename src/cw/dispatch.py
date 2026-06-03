"""Tick-based dispatch loop: claim pending TicketTasks and spawn Claude sessions."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult, parse_stdout
from cw.config import load_clients, load_orchestrator_config, load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, load_plan, save_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import StaleWorktreeError, WorktreeError
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import AUTO_DEV_LABEL_PREFIX, reconcile, ticket_id_for_session
from cw.spawn import spawn_create_impl
from cw.worktree import (
    check_not_main_checkout,
    create_worktree,
    is_main_behind_origin,
    remove_worktree,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        DevQueueStore,
        OrchestratorConfig,
        OrchestratorEvent,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient

_DISPATCH_CONSUMER = "dispatch"
_log = logging.getLogger(__name__)


def _claim_next_pending(
    client_name: str,
    *,
    priority_ticket_ids: list[str] | None = None,
) -> TicketTask | None:
    """Atomically claim the next PENDING task for a client.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* as RUNNING, saves, and returns it.
    Returns None if no pending task exists for the client.

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
                        and task.status == QueueItemStatus.PENDING
                    ):
                        task.status = QueueItemStatus.RUNNING
                        task.attempts += 1
                        save_dev_queue(store)
                        return task
        for task in store.tasks:
            if task.client == client_name and task.status == QueueItemStatus.PENDING:
                task.status = QueueItemStatus.RUNNING
                task.attempts += 1
                save_dev_queue(store)
                return task
    return None


def dispatch_tick(
    config: OrchestratorConfig,
    *,
    use_plan: bool = False,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
    emit: Callable[[str], None] | None = None,
    warned_stale: set[tuple[str, str]] | None = None,
) -> int:
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

    Returns:
        Number of sessions spawned during this tick.
    """
    resolved_native_daemon = native_daemon or get_native_daemon_client()
    try:
        reconcile()
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

    for client in clients.values():
        # --- Freshness gate ---
        # Check whether the client's local default branch is behind origin
        # before claiming any ticket.  Stale repos cause sessions to exit
        # immediately with local_main_diverged_from_origin, burning a slot.
        # On any error, log and proceed so a transient network issue never
        # blocks the whole loop.
        try:
            stale, _local_sha, _origin_sha, _behind = is_main_behind_origin(client)
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

        if stale:
            with dev_queue_lock():
                queue_store = load_dev_queue()
                stale_tasks = [
                    {"ticket_id": t.ticket_id, "client": client.name}
                    for t in queue_store.tasks
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
            continue

        # Count running daemon sessions for this client
        running_count = sum(
            1
            for s in state.sessions
            if s.client == client.name
            and s.origin == SessionOrigin.DAEMON
            and s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        )

        cap = config.per_client_max_parallel.get(
            client.name, config.default_max_parallel
        )

        priority_ids = plan_order_by_client.get(client.name)
        client_spawned = 0
        cap_full = running_count >= cap
        while running_count < cap:
            task: TicketTask | None = _claim_next_pending(
                client.name,
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

        if emit is not None:
            emit(
                f"{client.name}: spawned={client_spawned}"
                f" cap_full={int(cap_full)}"
            )

    return spawned


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost_usd to task.total_cost_usd, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_completed — normal headless path)
      2. session.last_result.get('cost_usd') (populated by persist_last_result —
         event-replay path where signal_completed did not run)

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

    # Persist sentinel-block summaries on Sessions whose completion event
    # carried captured stdout. Producer side (worker stdout capture) is
    # gated on the orchestrator P1.A wiring; this consumer is forward-
    # compatible with events that lack a ``stdout`` payload.
    for event in events:
        session_id = event.payload.get("session_id")
        stdout = event.payload.get("stdout")
        if isinstance(session_id, str) and isinstance(stdout, str):
            persist_last_result(session_id, stdout)

    # Advance cursor to the last event processed
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
    """
    config = load_orchestrator_config()

    if max_parallel is not None:
        clients = load_clients()
        overridden: dict[str, int] = dict.fromkeys(clients, max_parallel)
        config = config.model_copy(update={"per_client_max_parallel": overridden})

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()

    while True:
        consume_completed_sessions()
        dispatch_tick(
            config,
            use_plan=use_plan,
            parent=parent,
            native_daemon=resolved_native_daemon,
            emit=emit,
            warned_stale=warned_stale,
        )

        if once:
            return

        time.sleep(config.tick_interval_seconds)
