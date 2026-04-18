"""Tick-based dispatch loop: claim pending TicketTasks and spawn Claude sessions."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cw.cmux import get_cmux_adapter
from cw.config import load_clients, load_orchestrator_config, load_state
from cw.dev_queue import _lock, load_dev_queue, save_dev_queue
from cw.events import advance_cursor, read_events, record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.spawn import spawn_create_impl
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from cw.cmux import CmuxAdapter
    from cw.models import OrchestratorConfig, TicketTask

_DISPATCH_CONSUMER = "dispatch"


def _claim_next_pending(client_name: str) -> TicketTask | None:
    """Atomically claim the next PENDING task for a client.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* as RUNNING, saves, and returns it.
    Returns None if no pending task exists for the client.
    """
    with _lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.client == client_name and task.status == QueueItemStatus.PENDING:
                task.status = QueueItemStatus.RUNNING
                save_dev_queue(store)
                return task
    return None


def dispatch_tick(
    config: OrchestratorConfig,
    adapter: CmuxAdapter | None = None,
) -> int:
    """Run one dispatch tick.

    For each client that has pending TicketTasks, check how many DAEMON
    sessions are currently ACTIVE or IDLE and compare against the
    per-client cap from *config*.  If below the cap, claim one pending
    task and spawn a Claude session for it.

    Returns:
        Number of sessions spawned during this tick.
    """
    resolved_adapter = adapter or get_cmux_adapter()
    clients = load_clients()
    state = load_state()
    spawned = 0

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

        while running_count < cap:
            task = _claim_next_pending(client.name)
            if task is None:
                break

            branch = f"auto-dev/{task.ticket_id}"
            worktree_path = worktree_path_for(client, branch)
            worktree_path.mkdir(parents=True, exist_ok=True)

            prompt_path = worktree_path / ".cw-prompt.txt"
            prompt_path.write_text(f"/auto-dev {task.ticket_id}")

            label = f"auto-dev/{task.ticket_id}"
            session_id = spawn_create_impl(
                client=client,
                worktree=worktree_path,
                prompt_file=prompt_path,
                surface="split",
                label=label,
                adapter=resolved_adapter,
            )

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
    with _lock():
        store = load_dev_queue()
        for event in events:
            ticket_id = event.payload.get("ticket_id")
            if not ticket_id:
                continue
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.COMPLETED
                    completed += 1
                    break
        if completed:
            save_dev_queue(store)

    # Advance cursor to the last event processed
    advance_cursor(_DISPATCH_CONSUMER, events[-1].id)

    return completed


def run_dispatch_loop(
    *,
    max_parallel: int | None = None,
    once: bool = False,
    adapter: CmuxAdapter | None = None,
) -> None:
    """Run the dispatch loop, optionally overriding per-client concurrency caps.

    Args:
        max_parallel: If set, override all per-client caps with this value.
        once: If True, run a single tick and return immediately.
        adapter: Optional CmuxAdapter for testing.  Defaults to
            ``get_cmux_adapter()`` at call time.
    """
    config = load_orchestrator_config()

    if max_parallel is not None:
        clients = load_clients()
        overridden: dict[str, int] = dict.fromkeys(clients, max_parallel)
        config = config.model_copy(update={"per_client_max_parallel": overridden})

    resolved_adapter = adapter or get_cmux_adapter()

    while True:
        consume_completed_sessions()
        dispatch_tick(config, adapter=resolved_adapter)

        if once:
            return

        time.sleep(config.tick_interval_seconds)
