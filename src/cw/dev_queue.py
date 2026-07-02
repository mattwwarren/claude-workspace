"""Dev-queue management for orchestrator ticket dispatch."""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cw.atomic import atomic_write_text
from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
)
from cw.config import (
    dev_plan_file,
    dev_plan_lock,
    dev_queue_file,
    get_client,
)
from cw.config import (
    dev_queue_lock as _dev_queue_lock_file,
)
from cw.exceptions import (
    ApproveGateError,
    CwError,
    LaneMoveError,
    LaneNotFoundError,
    RequeueStageError,
    RequeueStateError,
    UnblockStateError,
)
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    DEV_QUEUE_SCHEMA_VERSION,
    DevQueueStore,
    DispatchPlan,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_WAIT_POLL_INTERVAL: int = 5

_TERMINAL_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
        QueueItemStatus.BLOCKED_ON_USER,
    ]
)

# Statuses that should stamp disposition/pr_url/completed_at on the task.
_TERMINAL_DISPOSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.COMPLETED,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.FAILED,
    ]
)

# Statuses that should clear disposition/pr_url/completed_at (requeue/cancel).
_RESET_DISPOSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.PENDING,
        QueueItemStatus.CANCELLED,
    ]
)


def transition_task_status(
    task: TicketTask,
    new_status: QueueItemStatus,
    *,
    disposition: str | None = None,
    pr_url: str | None = None,
) -> None:
    """Single authority for TicketTask status transitions. Mutates in place.

    Stamps disposition/pr_url/completed_at on terminal transitions
    (COMPLETED/BLOCKED_ON_USER/FAILED); clears them on PENDING/CANCELLED
    (requeue/cancel).  Companion field resets (session_id, stage_base_ref)
    stay at call sites.  GitHub #310.
    """
    task.status = new_status
    if new_status in _TERMINAL_DISPOSITION_STATUSES:
        task.disposition = disposition
        task.pr_url = pr_url
        task.completed_at = datetime.now(UTC)
    elif new_status in _RESET_DISPOSITION_STATUSES:
        task.disposition = None
        task.pr_url = None
        task.completed_at = None


_VERBATIM_DISPOSITION_STATUSES: frozenset[str] = (
    STAGE_SUCCESS_STATUSES
    | frozenset({"no_op", "merge_pending"})
    | STAGE_FAILURE_STATUSES
    | PAUSED_FOR_USER_INPUT_STATUSES
)


def _derive_disposition(status: str | None) -> str | None:
    """Map an AutoDevResult status to the task-level disposition string.

    Used alongside transition_task_status at terminal call sites to record
    the sentinel status verbatim (for operator-visible batch health).  GitHub #310.
    """
    if status is None:
        return "abandoned"
    if status in _VERBATIM_DISPOSITION_STATUSES:
        return status
    return "abandoned"


def _extract_pr_url(last_result: dict[str, object] | None) -> str | None:
    """Extract the PR URL from an AutoDevResult dict, or None if absent."""
    if last_result is None:
        return None
    pr = last_result.get("pr")
    if isinstance(pr, dict):
        url = pr.get("url")
        return str(url) if url is not None else None
    return None


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dev queue."""
    dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
    fd = _dev_queue_lock_file().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# Public alias for callers that need the dev-queue lock directly (e.g. the
# reconciler, which needs load → mutate → save around a RUNNING→PENDING
# revert). Prefer higher-level helpers like ``add_ticket`` when available.
dev_queue_lock = _lock


@contextlib.contextmanager
def _plan_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dispatch plan."""
    dev_plan_file().parent.mkdir(parents=True, exist_ok=True)
    fd = dev_plan_lock().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def plan_path() -> Path:
    """Return the path to the persisted dispatch plan file."""
    return dev_plan_file()


def save_plan(plan: DispatchPlan) -> Path:
    """Persist a DispatchPlan to disk under the plan file lock.

    Returns the path the plan was written to.
    """
    with _plan_lock():
        path = dev_plan_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, plan.model_dump_json(indent=2))
    return path


def load_plan() -> DispatchPlan | None:
    """Load the persisted DispatchPlan, returning None if missing.

    Returns None if the plan file does not exist or fails validation.
    Does not raise on validation errors — callers should fall back to
    enqueue order when None is returned.
    """
    path = dev_plan_file()
    if not path.exists():
        return None
    try:
        return DispatchPlan.model_validate_json(path.read_text())
    except (ValueError, OSError):
        return None


def _fill_task_cost_default(task_raw: dict[str, Any]) -> None:
    """Fill total_cost_usd introduced in dev-queue schema v2."""
    if "total_cost_usd" not in task_raw:
        task_raw["total_cost_usd"] = None


def _fill_lane_default(task_raw: dict[str, Any]) -> None:
    """Fill lane introduced in dev-queue schema v3."""
    if "lane" not in task_raw:
        task_raw["lane"] = DEFAULT_LANE


def _fill_task_stage_default(task_raw: dict[str, Any]) -> None:
    """Fill stage introduced in dev-queue schema v4 (GitHub #612). Idempotent."""
    if "stage" not in task_raw:
        task_raw["stage"] = DEFAULT_STAGE.value


def _fill_task_stage_base_ref_default(task_raw: dict[str, Any]) -> None:
    """Fill stage_base_ref from dev-queue schema v4 (GitHub #612). Idempotent."""
    if "stage_base_ref" not in task_raw:
        task_raw["stage_base_ref"] = None


def _fill_disposition_default(task_raw: dict[str, Any]) -> None:
    """Fill disposition introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "disposition" not in task_raw:
        task_raw["disposition"] = None


def _fill_pr_url_default(task_raw: dict[str, Any]) -> None:
    """Fill pr_url introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "pr_url" not in task_raw:
        task_raw["pr_url"] = None


def _fill_task_completed_at_default(task_raw: dict[str, Any]) -> None:
    """Fill completed_at introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "completed_at" not in task_raw:
        task_raw["completed_at"] = None


def _fill_regress_attempts_default(task_raw: dict[str, Any]) -> None:
    """Fill regress_attempts introduced in schema v6 (GitHub #770). Idempotent."""
    if "regress_attempts" not in task_raw:
        task_raw["regress_attempts"] = 0


def _fill_spawn_error_backoff_default(task_raw: dict[str, Any]) -> None:
    """Fill spawn_error_count/next_eligible_at introduced in schema v7 (GitHub #868).

    Idempotent."""
    if "spawn_error_count" not in task_raw:
        task_raw["spawn_error_count"] = 0
    if "next_eligible_at" not in task_raw:
        task_raw["next_eligible_at"] = None


def migrate_dev_queue(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw dev_queue.json payload into a currently-valid shape."""
    tasks = raw.get("tasks")
    if isinstance(tasks, list):
        for task_raw in tasks:
            if isinstance(task_raw, dict):
                _fill_task_cost_default(task_raw)
                _fill_lane_default(task_raw)
                _fill_task_stage_default(task_raw)
                _fill_task_stage_base_ref_default(task_raw)
                _fill_disposition_default(task_raw)
                _fill_pr_url_default(task_raw)
                _fill_task_completed_at_default(task_raw)
                _fill_regress_attempts_default(task_raw)
                _fill_spawn_error_backoff_default(task_raw)
    raw["schema_version"] = DEV_QUEUE_SCHEMA_VERSION
    return raw


def load_dev_queue() -> DevQueueStore:
    """Load the dev queue from disk, returning an empty store if missing."""
    path = dev_queue_file()
    if not path.exists():
        return DevQueueStore()
    raw = json.loads(path.read_text())
    return DevQueueStore.model_validate(migrate_dev_queue(raw))


def save_dev_queue(store: DevQueueStore) -> None:
    """Persist the dev queue to disk atomically."""
    path = dev_queue_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, store.model_dump_json(indent=2))


def add_ticket(task: TicketTask) -> bool:
    """Enqueue a TicketTask, acquiring the file lock atomically.

    Returns True if the task was inserted, False if a task with the same
    (client, ticket_id) is already PENDING, RUNNING, COMPLETED, or CANCELLED
    (deduplication guard — includes terminal siblings to prevent churn, #876).

    Raises:
        LaneNotFoundError: if task.lane is not declared for the client.
    """
    _blocked = {
        QueueItemStatus.PENDING,
        QueueItemStatus.RUNNING,
        QueueItemStatus.COMPLETED,
        QueueItemStatus.CANCELLED,
    }
    with _lock():
        try:
            client_cfg = get_client(task.client)
        except CwError:
            pass  # Unknown client — lane validation deferred to dispatch
        else:
            declared_lane_names = [ln.name for ln in client_cfg.effective_lanes]
            if task.lane not in declared_lane_names:
                msg = (
                    f"Lane '{task.lane}' is not declared for client '{task.client}'."
                    f" Declared lanes: {', '.join(declared_lane_names)}."
                    f" Run: cw lane add {task.client} {task.lane}"
                )
                raise LaneNotFoundError(msg)
        store = load_dev_queue()
        for existing in store.tasks:
            if (
                existing.client == task.client
                and existing.ticket_id == task.ticket_id
                and existing.status in _blocked
            ):
                return False
        store.tasks.append(task)
        save_dev_queue(store)
    return True


def remove_ticket(ticket_id: str, client: str, *, remove_all: bool = False) -> None:
    """Remove one (or all) matching TicketTask(s) from the dev queue.

    Raises CwError when no task matches.  Raises CwError when multiple tasks
    match and *remove_all* is False.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        ]
        n = len(matches)
        if n == 0:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'."
            )
            raise CwError(msg)
        if n > 1 and not remove_all:
            msg = (
                f"Multiple dev-queue tasks ({n}) match ticket '{ticket_id}' in"
                f" client '{client}'; pass --all to remove all."
            )
            raise CwError(msg)
        match_set = {id(m) for m in matches}
        store.tasks = [t for t in store.tasks if id(t) not in match_set]
        save_dev_queue(store)


def cancel_ticket(ticket_id: str, client: str) -> list[str | None]:
    """Mark a TicketTask as CANCELLED, clearing its session_id.

    Returns the list of session_ids that were cleared (one per cancelled task).
    Raises CwError when no task matches. Idempotent for already-CANCELLED tasks.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        ]
        if not matches:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'."
            )
            raise CwError(msg)
        cleared: list[str | None] = []
        changed = False
        for task in matches:
            if task.status == QueueItemStatus.CANCELLED:
                continue
            cleared.append(task.session_id)
            transition_task_status(task, QueueItemStatus.CANCELLED)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    return cleared


def cancel_task_for_session(session_id: str) -> bool:
    """Mark the RUNNING TicketTask that owns *session_id* as CANCELLED.

    Returns True if a task was cancelled, False if none matched.
    Used by _spawn_close_impl to atomically preempt the dispatcher before
    the session is marked COMPLETED. See GitHub issue #317.
    """
    with _lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.session_id == session_id and task.status == QueueItemStatus.RUNNING:
                transition_task_status(task, QueueItemStatus.CANCELLED)
                task.session_id = None
                save_dev_queue(store)
                return True
    return False


_UNMOVABLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER]
)


def move_ticket(ticket_id: str, client_name: str, to_lane: str) -> str:
    """Move a pending ticket to a different lane.

    Returns the previous lane name (from_lane) for event emission by the caller.

    Raises:
        CwError: if no matching task is found for (ticket_id, client_name).
        LaneNotFoundError: if to_lane is not declared for the client.
        LaneMoveError: if the task status is RUNNING or BLOCKED_ON_USER.

    Note: record_event is NOT called here — the CLI layer fires TICKET_MOVED.
    """
    with _lock():
        store = load_dev_queue()
        task = next(
            (
                t
                for t in store.tasks
                if t.ticket_id == ticket_id and t.client == client_name
            ),
            None,
        )
        if task is None:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client_name}'."
            )
            raise CwError(msg)

        client = get_client(client_name)
        declared_lane_names = [ln.name for ln in client.effective_lanes]
        if to_lane not in declared_lane_names:
            msg = (
                f"Lane '{to_lane}' is not declared for client '{client_name}'."
                f" Declared lanes: {', '.join(declared_lane_names)}."
                f" Run: cw lane add {client_name} {to_lane}"
            )
            raise LaneNotFoundError(msg)

        if task.status in _UNMOVABLE_STATUSES:
            msg = (
                f"Cannot move ticket '{ticket_id}': task is {task.status.value}."
                " Only PENDING tasks can be moved between lanes."
            )
            raise LaneMoveError(msg)

        from_lane = task.lane
        task.lane = to_lane
        save_dev_queue(store)
    return from_lane


def clear_tickets(client: str, status: QueueItemStatus | None = None) -> int:
    """Remove all TicketTasks for *client*, optionally filtered by *status*.

    Returns the number of tasks removed.
    """
    with _lock():
        store = load_dev_queue()
        if status is None:
            kept = [t for t in store.tasks if t.client != client]
        else:
            kept = [
                t
                for t in store.tasks
                if not (t.client == client and t.status == status)
            ]
        removed = len(store.tasks) - len(kept)
        store.tasks = kept
        save_dev_queue(store)
    return removed


def resolve_client(
    ticket_id: str,
    config: OrchestratorConfig,
    client_override: str | None,
) -> str:
    """Resolve the target client for a ticket.

    Resolution order:
    1. ``client_override`` (--client flag) if provided
    2. Prefix map: ``GEN-100`` -> prefix ``GEN`` -> client name
    3. Raise ``CwError`` if neither resolves
    """
    if client_override is not None:
        return client_override

    # Extract prefix: everything before the first '-'
    if "-" in ticket_id:
        prefix = ticket_id.split("-", maxsplit=1)[0]
        if prefix in config.linear_prefix_map:
            return config.linear_prefix_map[prefix]

    msg = (
        f"Cannot resolve client for ticket '{ticket_id}'."
        " Use --client to specify, or add the prefix to linear_prefix_map in"
        " ~/.claude-workspace/orchestrator.yaml."
    )
    raise CwError(msg)


def list_tickets(client: str | None = None) -> list[TicketTask]:
    """Return tickets from the dev queue, optionally filtered by client."""
    store = load_dev_queue()
    if client is None:
        return list(store.tasks)
    return [t for t in store.tasks if t.client == client]


def _find_ticket(store: DevQueueStore, ticket_id: str, client: str) -> TicketTask:
    """Return the TicketTask matching (ticket_id, client) or raise CwError.

    Selection priority: PENDING/RUNNING (live, newest created_at) →
    BLOCKED_ON_USER (newest created_at) → terminal (newest created_at).
    Re-resolved on every call — callers that poll (e.g. dev_queue_wait)
    pick up re-enqueued tasks on the next tick automatically.

    Emits a one-time stderr warning when multiple live PENDING/RUNNING tasks
    exist for the same (ticket_id, client).
    """
    # Why: add-after-terminal creates duplicate (client, ticket_id) rows.
    # Returning the oldest terminal record would cause wait to resolve
    # immediately on a stale status while a fresh RUNNING task is live.
    import click

    matches = [
        t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
    ]
    if not matches:
        msg = f"No dev-queue task found for ticket '{ticket_id}' in client '{client}'."
        raise CwError(msg)

    live = [
        t
        for t in matches
        if t.status in {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    ]
    if live:
        if len(live) > 1:
            click.echo(
                f"Warning: {len(live)} live tasks for ticket '{ticket_id}' "
                f"in client '{client}'; binding to newest.",
                err=True,
            )
        return max(live, key=lambda t: t.created_at)

    blocked = [t for t in matches if t.status == QueueItemStatus.BLOCKED_ON_USER]
    if blocked:
        return max(blocked, key=lambda t: t.created_at)

    return max(matches, key=lambda t: t.created_at)


def consume_completed_sessions() -> int:
    """Thin wrapper around dispatch.consume_completed_sessions.

    Exists as a named module-level function so that tests can monkeypatch
    ``cw.dev_queue.consume_completed_sessions`` without depending on a
    module-level circular import.  The real import is deferred to call time
    to break the dev_queue ↔ dispatch circular dependency.
    """
    from cw.dispatch import consume_completed_sessions as _impl

    return _impl()


def wait_for_terminal(
    ticket_id: str,
    client: str,
    *,
    timeout: float,
    poll_interval: float = _WAIT_POLL_INTERVAL,
) -> TicketTask:
    """Block until the given ticket reaches a terminal status.

    Calls consume_completed_sessions() once per poll tick before reading the
    queue, so it works whether or not a dispatch loop is active.

    # Why: before #471 merges, TIMED_OUT-but-PR-merged tickets may stay PENDING;
    # wait will then hit --timeout (exit 124) rather than returning COMPLETED.

    Terminal statuses: COMPLETED, FAILED, CANCELLED, BLOCKED_ON_USER.
    Raises CwError if the ticket is not found.
    Raises TimeoutError if *timeout* seconds elapse before a terminal status.
    """
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client)
    if task.status in _TERMINAL_STATUSES:
        return task

    deadline = time.monotonic() + timeout
    while True:
        consume_completed_sessions()
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client)
        if task.status in _TERMINAL_STATUSES:
            return task
        if time.monotonic() >= deadline:
            raise TimeoutError
        time.sleep(poll_interval)


def _advance_task_pointer(task: TicketTask, stages: list[Stage]) -> None:
    """Advance task to the next pipeline stage (no status precondition check).

    Mutates task in-place. The caller is responsible for any precondition checks.
    Does NOT check current status — approve path calls this directly;
    _stage_advance retains its RUNNING assert and calls this after.
    """
    idx = stages.index(task.stage)
    task.stage = stages[idx + 1]
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None  # R6: clear session_id on advance
    task.stage_base_ref = None  # cleared so next spawn stamps fresh ref


def _stage_regress(task: TicketTask, target_stage: Stage) -> None:
    """Regress task to a prior pipeline stage for self-heal.

    Mutates task in-place: sets stage to target_stage, increments
    regress_attempts, reverts status to PENDING, and clears session anchors.
    worktree_path is preserved so the next impl session resumes the branch.
    Caller is responsible for stage selection and regress-cap enforcement.
    See GitHub #770.
    """
    task.stage = target_stage
    task.regress_attempts += 1
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None
    task.stage_base_ref = None


def approve_ticket(ticket_id: str, client_name: str) -> dict[str, str]:
    """Approve a plan_pending_approval or review_pending_approval gate.

    Returns dict with from_stage, to_stage, ticket_id, client for event emission.

    Raises:
        ApproveGateError: if ticket is not at an approval gate, session is missing,
            last_result is absent, or last_result status is not an approval gate.
        CwError: if no matching task is found.
    """
    from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES
    from cw.config import load_state

    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)

        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            msg = (
                f"Cannot approve ticket '{ticket_id}': status is {task.status.value!r},"
                " expected BLOCKED_ON_USER. Use 'requeue' to re-run a stage."
            )
            raise ApproveGateError(msg)

        state = load_state()
        session = None
        if task.session_id is not None:
            session = state.find_by_name_or_id(task.session_id)

        if session is None:
            msg = (
                f"Cannot approve ticket '{ticket_id}': session not found"
                f" (session_id={task.session_id!r}). The session may have been"
                " cleaned up. Use 'requeue' to re-run the stage."
            )
            raise ApproveGateError(msg)

        if (
            session.last_result is None
            or session.last_result.get("status") not in SCOPE_GATED_APPROVAL_STATUSES
        ):
            actual = session.last_result.get("status") if session.last_result else None
            msg = (
                f"Cannot approve ticket '{ticket_id}': not at an approval gate"
                f" (last_result status={actual!r})."
                " Expected one of: plan_pending_approval, review_pending_approval."
                " Use 'requeue' if you want to re-run the current stage."
            )
            raise ApproveGateError(msg)

        client_cfg = get_client(client_name)
        stages = client_cfg.pipeline.stages

        if task.stage not in stages:
            msg = (
                f"Cannot approve ticket '{ticket_id}':"
                f" stage {task.stage!r} not in pipeline."
            )
            raise ApproveGateError(msg)

        if task.stage == stages[-1]:
            msg = (
                f"Cannot approve ticket '{ticket_id}':"
                f" already at terminal stage {task.stage!r}."
            )
            raise ApproveGateError(msg)

        from_stage = task.stage.value
        _advance_task_pointer(task, stages)
        to_stage = task.stage.value

        save_dev_queue(store)

    return {
        "from_stage": from_stage,
        "to_stage": to_stage,
        "ticket_id": ticket_id,
        "client": client_name,
    }


def requeue_ticket(
    ticket_id: str,
    client_name: str,
    stage_override: str | None = None,
) -> dict[str, str]:
    """Requeue a BLOCKED_ON_USER ticket, optionally at a specific stage.

    Returns dict with from_stage, to_stage, ticket_id, client for event emission.

    Raises:
        RequeueStateError: if ticket is not BLOCKED_ON_USER.
        RequeueStageError: if stage_override would regress the ticket or is not
            in the client pipeline.
        CwError: if no matching task is found.
    """
    with _lock():
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)

        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            msg = (
                f"Cannot requeue ticket '{ticket_id}': status is {task.status.value!r},"
                " expected BLOCKED_ON_USER."
            )
            raise RequeueStateError(msg)

        client_cfg = get_client(client_name)
        stages = client_cfg.pipeline.stages

        from_stage = task.stage

        if stage_override is not None:
            target_stage = Stage(stage_override)
            if target_stage not in stages:
                msg = (
                    f"Stage '{stage_override}' is not in the pipeline"
                    f" for client '{client_name}'."
                )
                raise RequeueStageError(msg)
            current_idx = stages.index(task.stage)
            target_idx = stages.index(target_stage)
            if target_idx < current_idx:
                msg = (
                    f"Cannot requeue ticket '{ticket_id}'"
                    f" to stage '{stage_override}':"
                    f" that would regress from '{task.stage.value}'."
                    " Only same-stage or forward advancement is allowed."
                )
                raise RequeueStageError(msg)
            task.stage = target_stage

        transition_task_status(task, QueueItemStatus.PENDING)
        task.session_id = None
        task.stage_base_ref = None
        task.regress_attempts = 0

        to_stage = task.stage
        save_dev_queue(store)

    return {
        "from_stage": from_stage.value,
        "to_stage": to_stage.value,
        "ticket_id": ticket_id,
        "client": client_name,
    }


def unblock_ticket(ticket_id: str, client_name: str) -> dict[str, str]:
    """Clear salvage/park markers and requeue a SALVAGE_PARKED ticket.

    Returns dict with ticket_id, client for event emission.

    Raises:
        UnblockStateError: if ticket is not park-marked (reap_reason != SALVAGE_PARKED).
        CwError: if no matching task is found.
    """
    from cw.config import load_state, save_state, sessions_lock
    from cw.models import ReapReason

    # Fast-fail pre-check outside any lock; re-validated under sessions_lock below.
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)

    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot unblock ticket '{ticket_id}': status is {task.status.value!r},"
            " expected BLOCKED_ON_USER."
        )
        raise UnblockStateError(msg)

    session_id = task.session_id

    # Why: sessions_lock outer, dev_queue_lock inner — canonical dual-lock ordering.
    # Both saves happen under the outer lock so sessions.json is never written unless
    # dev_queue.json succeeds first (safe-fail direction for partial-commit scenarios).
    with sessions_lock():
        state = load_state()
        session = None
        if session_id is not None:
            session = state.find_by_name_or_id(session_id)

        if session is None or session.reap_reason != ReapReason.SALVAGE_PARKED:
            if session is None:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session not found"
                    f" (session_id={session_id!r}). The park-marked precondition"
                    " cannot be confirmed."
                )
            else:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session is not park-marked"
                    f" (reap_reason={session.reap_reason!r},"
                    " expected SALVAGE_PARKED)."
                )
            raise UnblockStateError(msg)

        with _lock():
            store = load_dev_queue()
            task = _find_ticket(store, ticket_id, client_name)
            if task.status != QueueItemStatus.BLOCKED_ON_USER:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': status changed to"
                    f" {task.status.value!r} concurrently. Re-check and retry."
                )
                raise UnblockStateError(msg)
            transition_task_status(task, QueueItemStatus.PENDING)
            task.session_id = None
            task.stage_base_ref = None
            save_dev_queue(store)

        session.last_result = None
        session.reap_reason = None
        save_state(state)

    return {"ticket_id": ticket_id, "client": client_name}
