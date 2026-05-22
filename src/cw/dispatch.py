"""Tick-based dispatch loop: claim pending TicketTasks and spawn Claude sessions."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult, parse_stdout
from cw.cmux import get_cmux_adapter
from cw.config import load_clients, load_orchestrator_config, load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, load_plan, save_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile import AUTO_DEV_LABEL_PREFIX, reconcile, ticket_id_for_session
from cw.spawn import spawn_create_impl
from cw.worktree import create_worktree

if TYPE_CHECKING:
    from cw.cmux import CmuxAdapter
    from cw.models import OrchestratorConfig, TicketTask

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
                        save_dev_queue(store)
                        return task
        for task in store.tasks:
            if task.client == client_name and task.status == QueueItemStatus.PENDING:
                task.status = QueueItemStatus.RUNNING
                save_dev_queue(store)
                return task
    return None


def dispatch_tick(
    config: OrchestratorConfig,
    adapter: CmuxAdapter | None = None,
    *,
    use_plan: bool = False,
    parent: str | None = None,
) -> int:
    """Run one dispatch tick.

    For each client that has pending TicketTasks, check how many DAEMON
    sessions are currently ACTIVE or IDLE and compare against the
    per-client cap from *config*.  If below the cap, claim one pending
    task and spawn a Claude session for it.

    Args:
        config: Orchestrator config (per-client caps, tick interval).
        adapter: Optional CmuxAdapter for testing.
        use_plan: If True, respect the persisted DispatchPlan ordering.
        parent: Optional parent session ID. When set, every spawned
            worker is linked to it (``parent_session_id`` +
            bidirectional ``worker_session_ids``) so ``cw orchestrate
            workers`` can list dispatched workers as first-class
            sessions.

    Returns:
        Number of sessions spawned during this tick.
    """
    resolved_adapter = adapter or get_cmux_adapter()
    try:
        reconcile(resolved_adapter)
    except Exception:
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
        # Count running daemon sessions for this client
        running_count = sum(
            1
            for s in state.sessions
            if s.client == client.name
            and s.origin == SessionOrigin.DAEMON
            and s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        )

        cap = config.per_client_max_parallel.get(client.name, 1)

        priority_ids = plan_order_by_client.get(client.name)
        while running_count < cap:
            task: TicketTask | None = _claim_next_pending(
                client.name,
                priority_ticket_ids=priority_ids,
            )
            if task is None:
                break

            branch = f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}"
            # Create a real git worktree (idempotent — returns existing path
            # if already created). Replaces a previous bug where dispatch
            # made an empty directory and relied on ``claude -w`` to turn
            # it into a worktree, which never worked because that flag
            # takes a name rather than a path.
            worktree_path = create_worktree(client, branch)

            label = f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}"
            session_id = spawn_create_impl(
                client=client,
                worktree=worktree_path,
                prompt=f"/auto-dev {task.ticket_id} --headless",
                surface="split",
                label=label,
                adapter=resolved_adapter,
                parent=parent,
                ticket_id=task.ticket_id,
            )

            # Stamp session_id on the queued task so the completion consumer
            # can match SESSION_COMPLETED events to the correct (current)
            # session and reject stale events from prior crashed sessions
            # for the same ticket. See GitHub issue #97.
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

            running_count += 1
            spawned += 1

    return spawned


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

    completed = 0
    with dev_queue_lock():
        store = load_dev_queue()
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
                completed += 1
                break
        if completed:
            save_dev_queue(store)

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
    adapter: CmuxAdapter | None = None,
    use_plan: bool = False,
    parent: str | None = None,
) -> None:
    """Run the dispatch loop, optionally overriding per-client concurrency caps.

    Args:
        max_parallel: If set, override all per-client caps with this value.
        once: If True, run a single tick and return immediately.
        adapter: Optional CmuxAdapter for testing.  Defaults to
            ``get_cmux_adapter()`` at call time.
        use_plan: If True, load the persisted DispatchPlan and use its
            ordering to claim tasks.  Falls back to enqueue order when no
            plan is found (load_plan returns None).
        parent: Optional orchestrator session ID. Threaded into each
            dispatch tick so spawned workers are linked back to the
            caller's session.
    """
    config = load_orchestrator_config()

    if max_parallel is not None:
        clients = load_clients()
        overridden: dict[str, int] = dict.fromkeys(clients, max_parallel)
        config = config.model_copy(update={"per_client_max_parallel": overridden})

    resolved_adapter = adapter or get_cmux_adapter()

    while True:
        consume_completed_sessions()
        dispatch_tick(
            config,
            adapter=resolved_adapter,
            use_plan=use_plan,
            parent=parent,
        )

        if once:
            return

        time.sleep(config.tick_interval_seconds)
