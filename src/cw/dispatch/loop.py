"""The dispatch event loop: session-completion consumption and ``run_dispatch_loop``.

Part of the ``cw.dispatch`` package split (#1311, part 2): the long-running
event loop, the SESSION_COMPLETED consumer, and the version-drift/back-off
plumbing, carved out of ``_legacy`` so the loop and the per-tick path live in
separate submodules."""

from __future__ import annotations

import contextlib
import importlib.metadata
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import yaml

from cw.auto_dev_result import (
    AutoDevResult,
    parse_stdout,
)
from cw.config import (
    load_clients,
    load_effective_clients,
    load_effective_config,
    load_state,
    load_usage_limited_until,
    save_state,
    save_usage_limited_until,
    sessions_lock,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import (
    ConfigValidationError,
    VersionDriftError,
)
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.pr_hydrate import hydrate_pr_states
from cw.reconcile import (
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorEvent,
    )
    from cw.native_daemon import NativeDaemonClient
from cw.dispatch._legacy import _accumulate_task_cost, apply_staged_decision
from cw.dispatch.tick import dispatch_tick

_log = logging.getLogger("cw.dispatch")


_DISPATCH_CONSUMER = "dispatch"


# Duplicated from cw.doctor; importing from there creates a circular dep.
# A future cw.const cleanup can consolidate these.
_CW_PACKAGE_NAME: str = "claude-workspace"


def _resolve_loaded_version() -> str:
    """Capture the installed version at import time for drift detection."""
    try:
        return importlib.metadata.version(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


# Captured at import time; remains the version that was actually loaded.
_LOADED_VERSION: str = _resolve_loaded_version()


def _apply_events_to_store(
    store: DevQueueStore,
    events: list[OrchestratorEvent],
    clients: dict[str, ClientConfig],
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
            last_result = (
                session.last_result
                if session is not None and isinstance(session.last_result, dict)
                else None
            )
            status = last_result.get("status") if last_result is not None else None

            # #1019: a stage-mismatch refusal is a true no-op (Pre-flight
            # Resolution #4) -- skip cost accumulation and the completed
            # count so a refused/stale sentinel doesn't mutate task state
            # or trigger save_dev_queue below.
            if apply_staged_decision(task, status, last_result, clients):
                sid = event_session_id if isinstance(event_session_id, str) else None
                _accumulate_task_cost(task, sid)
                completed += 1
            break
    if completed:
        save_dev_queue(store)
    return completed


def consume_completed_sessions() -> int:
    """Process session.completed events from the dispatch inbox.

    Reads new SESSION_COMPLETED events from the inbox since the last cursor
    position for the "dispatch" consumer. For each event that carries a
    ``ticket_id`` in its payload, the corresponding TicketTask (if found in
    RUNNING state) is marked COMPLETED.

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

    # Gap class (#867, severity=low): persist_last_result writes sessions.json
    # under sessions_lock; the dev-queue mutation below is a separate file.
    # A crash here leaves last_result updated but the task RUNNING.  The
    # un-advanced cursor causes self-healing reprocessing on the next tick.
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


TICK_STALE_SECONDS = 90  # 3x default tick_interval_seconds=30


def _merge_persisted_usage_limited_until(
    usage_limited_until: datetime | None,
) -> datetime | None:
    """Merge the on-disk usage-limit sidecar into the in-memory window (#1346).

    Re-read every tick: the pre-loop load in :func:`run_dispatch_loop` only
    protects a fresh restart (#804) -- it is never revisited, so this
    process's in-memory window silently diverges from what any OTHER
    dispatch process (or this same process's own earlier tick) has
    persisted. Merge rather than overwrite: take the later of {in-memory,
    on-disk}, treating None as "no window". ``load_usage_limited_until()``
    returns None for a file that is absent, unreadable, malformed, OR merely
    expired (config.py) -- a bare assignment would let a transient disk-read
    failure silently reopen the spawn gate mid-backoff. A read must never
    shorten an active window.
    """
    persisted_usage_limited_until = load_usage_limited_until()
    if persisted_usage_limited_until is not None and (
        usage_limited_until is None
        or persisted_usage_limited_until > usage_limited_until
    ):
        return persisted_usage_limited_until
    return usage_limited_until


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
    # Track wave-collision pairs already warned; prevents duplicate events
    # for long-running in-flight task pairs across multiple ticks (#784).
    warned_collision: set[frozenset[str]] = set()
    # Back-off window: loaded from the persisted sidecar so a loop restart after
    # a code merge honours an active backoff rather than re-opening the spawn gate
    # immediately (#804).
    usage_limited_until: datetime | None = load_usage_limited_until()

    try:
        while True:
            try:
                config = load_effective_config()
                if max_parallel is not None:
                    clients = load_clients()
                    overridden = dict.fromkeys(clients, max_parallel)
                    config = config.model_copy(
                        update={"per_client_ceiling": overridden}
                    )
            except (yaml.YAMLError, ConfigValidationError):
                _log.warning("dispatch: config reload failed; using last-good config")

            # Re-read the shared back-off sidecar every tick (#1346) -- see
            # _merge_persisted_usage_limited_until for why this must merge
            # rather than overwrite.
            usage_limited_until = _merge_persisted_usage_limited_until(
                usage_limited_until
            )

            consume_completed_sessions()
            try:
                hydrate_pr_states(config)
            except Exception:  # noqa: BLE001
                # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
                # 1. hydrate_pr_states shells out to ``gh pr view`` — failure
                #    modes include subprocess crash, JSON decode, lock I/O.
                # 2. Logging: _log.exception captures the full traceback.
                # 3. Non-critical: PR-state hydration is best-effort fallback
                #    polling (#929); skipping a pass just delays state refresh.
                # 4. Paired test: tests/test_dispatch.py
                #    TestRunDispatchLoopHydrationHook.
                _log.exception("pr-state hydration failed during tick; continuing")
            try:
                _installed = importlib.metadata.version(_CW_PACKAGE_NAME)
            except importlib.metadata.PackageNotFoundError:
                _installed = "0.0.0+unknown"
            if _installed != _LOADED_VERSION:
                _log.warning(
                    "dispatch: version drift detected (loaded=%s, installed=%s)"
                    " — exiting for reload",
                    _LOADED_VERSION,
                    _installed,
                )
                msg = "version drift detected; exiting for reload"
                raise VersionDriftError(msg)
            result = dispatch_tick(
                config,
                use_plan=use_plan,
                parent=parent,
                native_daemon=resolved_native_daemon,
                emit=emit,
                warned_stale=warned_stale,
                warned_fetch_fail=warned_fetch_fail,
                warned_collision=warned_collision,
                usage_limited_until=usage_limited_until,
                auto_ff=auto_ff,
                client_filter=client,
            )

            if result.usage_limit_detected and not once:
                usage_limited_until = datetime.now(UTC) + timedelta(
                    seconds=config.usage_limit_backoff_seconds
                )
                save_usage_limited_until(usage_limited_until)
                _log.warning(
                    "dispatch: usage limit detected; backing off until %s",
                    usage_limited_until,
                )

            if once:
                return

            time.sleep(config.tick_interval_seconds)
    finally:
        _exc = sys.exc_info()[1]
        _normal = _exc is None or isinstance(_exc, KeyboardInterrupt)
        _payload: dict[str, object] = {
            "normal": _normal,
            "exception_type": None if _normal else type(_exc).__name__,
        }
        with contextlib.suppress(Exception):
            if isinstance(_exc, VersionDriftError):
                _payload["reason"] = "version_drift"
                _payload["loaded_version"] = _LOADED_VERSION
                _payload["installed_version"] = importlib.metadata.version(
                    _CW_PACKAGE_NAME
                )
            record_event(
                OrchestratorEventType.DISPATCH_LOOP_EXITED,
                payload=_payload,
            )
