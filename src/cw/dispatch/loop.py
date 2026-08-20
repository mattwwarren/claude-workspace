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

from cw.codex_background import join_outstanding_codex_threads
from cw.config import (
    dispatch_loop_lock,
    load_clients,
    load_effective_clients,
    load_effective_config,
    load_state,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.dispatch_state import (
    clear_all_executor_blocked_markers,
    load_usage_limit_armed_at,
    load_usage_limited_until,
    save_usage_limit_armed_at,
    save_usage_limited_until,
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
    _CAUSE_USAGE_LIMIT,
    register_stale_dispatch_watched_prs,
    release_stale_gated_tasks,
    ticket_id_for_session,
)
from cw.reconcile.codex_boot import reap_orphaned_codex_sessions_at_boot

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        OrchestratorEvent,
    )
    from cw.native_daemon import NativeDaemonClient
from cw.dispatch.routing import _accumulate_task_cost, apply_staged_decision
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

    # ``last_result`` is populated by the RFC 0012 door at each producer's
    # write site BEFORE SESSION_COMPLETED is emitted -- the Stop hook via
    # ``emit_result_locked`` (A1), executors via ``_complete_session_via_door``
    # (A2), and reconcile's git-synthesis path (A3). The event itself carries
    # no result payload; _apply_events_to_store reads ``session.last_result``
    # from state directly. An absent last_result (a producer that hasn't
    # written through the door, or a session with no sentinel at all) still
    # routes conservative-safe via Rule 6 -> BLOCKED_ON_USER. The crash-window
    # gap this replaces (a door write landing but the task mutation below not
    # yet persisted) now sits at each producer's door-write-vs-task-routing
    # boundary rather than inside this consumer; still tracked by #867.
    with dev_queue_lock():
        store = load_dev_queue()
        clients = load_effective_clients()
        completed = _apply_events_to_store(store, events, clients=clients)
        # Advance cursor inside the dev-queue lock so the cursor never
        # moves past events whose queue mutations haven't been persisted yet.
        advance_cursor(_DISPATCH_CONSUMER, events[-1].id)

    return completed


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
    expired (dispatch_state.py) -- a bare assignment would let a transient disk-read
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


def _usage_limit_window_is_active(usage_limited_until: datetime | None) -> bool:
    """Is the usage-limit backoff window currently active (#1343)?

    Single source of truth for the "armed and not yet expired" check, shared
    by the loop's pre-loop seed and :func:`_handle_usage_limit_window_transition`
    -- mirrors the wall-clock check :func:`~cw.dispatch.tick.dispatch_tick`
    itself uses to gate spawning.
    """
    return usage_limited_until is not None and usage_limited_until > datetime.now(UTC)


def _emit_usage_limit_cleared(armed_at: datetime | None, cleared_at: datetime) -> None:
    """Emit ``dispatch.usage_limit_cleared`` for an armed->cleared transition (#1343).

    Scans ``session.timed_out`` events with ``cause=usage_limit_cutoff`` since
    *armed_at* (stateless re-scan -- no consumer cursor, since this must be
    re-evaluated fresh on every transition) to compute the affected cohort.
    When *armed_at* is None (persist failed, or the window predates this
    field existing), still emits with ``detected_at: null`` rather than
    skipping -- skipping would silently drop the correlating signal
    orchestrators need most; the cohort scan degrades to unbounded (all
    matching history) in that case, an accepted trade-off.
    """
    events = read_events(
        event_types=[OrchestratorEventType.SESSION_TIMED_OUT],
        since_ts=armed_at,
    )
    cutoff_events = [
        event for event in events if event.payload.get("cause") == _CAUSE_USAGE_LIMIT
    ]
    clients_affected = sorted(
        {
            event.payload["client"]
            for event in cutoff_events
            if isinstance(event.payload.get("client"), str)
        }
    )
    record_event(
        OrchestratorEventType.USAGE_LIMIT_CLEARED,
        {
            "clients_affected": clients_affected,
            "sessions_affected": len(cutoff_events),
            "detected_at": armed_at.isoformat() if armed_at is not None else None,
            "cleared_at": cleared_at.isoformat(),
        },
    )


def _handle_usage_limit_window_transition(
    was_active: bool,
    *,
    usage_limited_until: datetime | None,
    armed_at: datetime | None,
) -> bool:
    """Detect an armed->cleared usage-limit backoff transition; emit for it (#1343).

    Returns this tick's active state, for the caller to carry forward as
    ``was_active`` into the next tick's call. Fires
    :attr:`OrchestratorEventType.USAGE_LIMIT_CLEARED` exactly once -- on the
    tick that observes ``was_active and not now_active`` -- never per-client
    (a single event carries the full ``clients_affected`` cohort).
    ``usage_limited_until`` and ``armed_at`` are keyword-only: both are
    ``datetime | None``, and a positional call site could otherwise swap them
    silently past mypy. "Active" mirrors the wall-clock check
    :func:`~cw.dispatch.tick.dispatch_tick` itself uses to gate spawning
    (``usage_limited_until`` set AND still in the future) --
    ``usage_limited_until`` is never reset to None on natural expiry (only
    overwritten by a fresh detection), so "is not None" alone would never
    observe a transition.

    In-process-local detector state only (#1343 R4): the caller
    (:func:`run_dispatch_loop`) threads ``was_active``/``armed_at`` as plain
    locals, not a persisted latch -- this depends on the single-long-running-
    loop-per-fleet invariant (documented, not enforced; #1362, confirmed
    still OPEN). A second concurrent loop would each independently observe
    the same transition and double-emit.
    """
    now_active = _usage_limit_window_is_active(usage_limited_until)
    if was_active and not now_active:
        _emit_usage_limit_cleared(armed_at, cleared_at=datetime.now(UTC))
    return now_active


def _reload_tick_config(
    config: OrchestratorConfig, max_parallel: int | None
) -> OrchestratorConfig:
    """Reload effective config for one tick, applying a max_parallel override.

    Extracted from ``run_dispatch_loop`` (#1343) to buy back statement
    budget ahead of the usage-limit-cleared transition logic -- see Design
    §2 in the plan for the statement-budget arithmetic. On a reload failure
    (bad YAML / validation error), logs a warning and returns *config*
    unchanged (last-good config), matching the loop's prior inline behavior.
    """
    try:
        reloaded = load_effective_config()
        if max_parallel is not None:
            clients = load_clients()
            overridden = dict.fromkeys(clients, max_parallel)
            reloaded = reloaded.model_copy(update={"per_client_ceiling": overridden})
    except (yaml.YAMLError, ConfigValidationError):
        _log.warning("dispatch: config reload failed; using last-good config")
        return config
    return reloaded


def _check_version_drift() -> None:
    """Raise VersionDriftError if the installed package version has changed.

    Extracted from ``run_dispatch_loop`` (#1343) -- see Design §2 in the
    plan for the statement-budget arithmetic that motivated this split.
    Compares the installed version against ``_LOADED_VERSION`` (captured at
    import time); a mismatch means the process is running stale code after
    an in-place upgrade, so the loop should exit for a clean reload.
    """
    try:
        installed = importlib.metadata.version(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        installed = "0.0.0+unknown"
    if installed != _LOADED_VERSION:
        _log.warning(
            "dispatch: version drift detected (loaded=%s, installed=%s)"
            " — exiting for reload",
            _LOADED_VERSION,
            installed,
        )
        msg = "version drift detected; exiting for reload"
        raise VersionDriftError(msg)


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
    force: bool = False,
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
        force: When True, bypass the process-lifetime singleton lock (#1362)
            via ``contextlib.nullcontext()`` and log a WARNING on every entry.
            Escape hatch for a wedged/foreign holder; otherwise a second loop
            launch fails fast with ``DispatchLoopLockedError``.
    """
    if force:
        _log.warning(
            "dispatch: --force set; bypassing the dispatch-loop singleton lock"
            " (#1362) — a second concurrent loop can diverge per-process state"
        )
        lock_cm: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
    else:
        lock_cm = dispatch_loop_lock()
    with lock_cm:
        _run_dispatch_loop_body(
            max_parallel=max_parallel,
            once=once,
            use_plan=use_plan,
            parent=parent,
            native_daemon=native_daemon,
            emit=emit,
            auto_ff=auto_ff,
            client=client,
        )


def _run_pr_state_hydration_guarded(config: OrchestratorConfig) -> None:
    """Run ``hydrate_pr_states``, swallowing any failure (non-fatal tick step).

    Extracted from ``_run_dispatch_loop_body`` (PLR0915) -- also keeps its
    sibling ``_run_stale_gate_release_guarded`` symmetric as a paired helper.
    """
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


def _run_stale_dispatch_watch_registration_guarded() -> None:
    """Run ``register_stale_dispatch_watched_prs``, swallowing any failure.

    GitHub #1927. Third member of the same guarded-tick-step family as
    ``_run_pr_state_hydration_guarded`` / ``_run_stale_gate_release_guarded``,
    and deliberately sequenced between them: ``consume_completed_sessions``
    stamps ``blocked_on_pr`` (under ``dev_queue_lock``), this pass registers
    the watch for it, hydration fills that watch's ``pr_state`` on the SAME
    tick, and the release pass then observes the merged fact -- so a park
    whose PR is already merged clears in one tick rather than three.
    """
    try:
        register_stale_dispatch_watched_prs()
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. register_stale_dispatch_watched_prs shells out to
        #    ``git remote get-url origin`` and writes dev_queue.json under
        #    dev_queue_lock — failure modes include subprocess crash, lock
        #    I/O, and store-parse surprises.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical: registration is best-effort backfill; skipping a
        #    tick loses nothing, because the pass is a full retroactive
        #    rescan — the next tick re-derives the identical candidate set
        #    from unchanged on-disk state.
        # 4. Paired test: tests/test_dispatch.py
        #    TestRunDispatchLoopStaleDispatchWatchHook.
        _log.exception(
            "stale-dispatch watch registration failed during tick; continuing"
        )


def _run_stale_gate_release_guarded() -> None:
    """Run ``release_stale_gated_tasks``, swallowing any failure (GitHub #1713).

    Extracted from ``_run_dispatch_loop_body`` (PLR0915) alongside its sibling
    ``_run_pr_state_hydration_guarded``.
    """
    try:
        release_stale_gated_tasks()
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. release_stale_gated_tasks reads the events inbox and writes
        #    dev_queue.json under dev_queue_lock — failure modes include
        #    lock I/O and event-payload surprises.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical: this is a best-effort re-validation pass (#1713);
        #    skipping a tick just delays a stale-gate release, it does not
        #    lose the underlying pr.merged event -- the consumer cursor is
        #    only advanced after the corresponding mutation batch is
        #    durably persisted (save_dev_queue), so a mid-call failure here
        #    leaves the cursor untouched and the next tick's retry safely
        #    re-derives the identical release from the still-unconsumed
        #    event (see release_stale_gated_tasks's advance_cursor
        #    ordering).
        # 4. Paired test: tests/test_dispatch.py
        #    TestRunDispatchLoopStaleGateHook.
        _log.exception("stale-gate release failed during tick; continuing")


def _run_dispatch_loop_body(
    *,
    max_parallel: int | None,
    once: bool,
    use_plan: bool,
    parent: str | None,
    native_daemon: NativeDaemonClient | None,
    emit: Callable[[str], None] | None,
    auto_ff: bool,
    client: str | None,
) -> None:
    """The dispatch loop proper, run inside the singleton lock (#1362).

    Extracted from :func:`run_dispatch_loop` so the lock-acquisition wrapper
    stays small and the loop body is not re-indented under a ``with``. The
    caller owns lock acquisition/release; this function assumes it holds (or
    has deliberately bypassed) the lock.
    """
    config = load_effective_config()

    # Boot pass (#1727): a codex session still ACTIVE at process start means
    # the prior process died mid-review (crash/SIGKILL) — the one case the
    # shutdown join below cannot reach, since the thread died with its process.
    # Runs once, before the first tick, under the singleton lock (#1362).
    orphaned_codex = reap_orphaned_codex_sessions_at_boot()
    if orphaned_codex:
        _log.warning(
            "dispatch: %d codex session(s) were ACTIVE at process start;"
            " parked for operator inspection",
            orphaned_codex,
        )

    # Any marker on disk at this point is orphaned — no daemon thread survives
    # a process restart (#1742). Sibling of the reap above, deliberately kept
    # separate: parking an orphaned session and clearing a stale marker are
    # independent concerns.
    clear_all_executor_blocked_markers()

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()
    # Track fetch-fail-warn deduplication for persistently unreachable remotes.
    warned_fetch_fail: set[str] = set()
    # Track wave-collision pairs already warned; prevents duplicate events
    # for long-running in-flight task pairs across multiple ticks (#784).
    warned_collision: set[frozenset[str]] = set()
    # Track the SSH-key-gate operator error line dedup (#927); fleet-wide
    # sentinel, not per-client (see _SSH_KEY_WARN_SENTINEL).
    warned_ssh_key: set[str] = set()
    # Track the disk-pressure-gate operator WARN line dedup (#1887); keyed
    # per-client, not fleet-wide, since each client's worktree_base may sit
    # on its own mount.
    warned_disk_pressure: set[str] = set()
    # Back-off window: loaded from the persisted sidecar so a loop restart after
    # a code merge honours an active backoff rather than re-opening the spawn gate
    # immediately (#804).
    usage_limited_until: datetime | None = load_usage_limited_until()
    # #1343: in-process-local usage-limit-window transition detector state --
    # see _handle_usage_limit_window_transition's docstring for the R4
    # single-loop-invariant caveat.
    usage_limit_window_active: bool = _usage_limit_window_is_active(usage_limited_until)
    usage_limit_window_armed_at: datetime | None = load_usage_limit_armed_at()

    try:
        while True:
            config = _reload_tick_config(config, max_parallel)

            # Re-read the shared back-off sidecar every tick (#1346) -- see
            # _merge_persisted_usage_limited_until for why this must merge
            # rather than overwrite.
            usage_limited_until = _merge_persisted_usage_limited_until(
                usage_limited_until
            )
            # #1343 R3: skip the transition check in --once mode. A single
            # tick's pre-loop-loaded `usage_limit_window_active` could
            # otherwise appear to "lapse" within that one tick (e.g. a
            # window that was seconds from expiry at loop start), which
            # would violate --once's "never emits" contract -- the natural
            # multi-tick invariant this check relies on doesn't hold for a
            # single-tick run.
            if not once:
                usage_limit_window_active = _handle_usage_limit_window_transition(
                    usage_limit_window_active,
                    usage_limited_until=usage_limited_until,
                    armed_at=usage_limit_window_armed_at,
                )

            consume_completed_sessions()
            _run_stale_dispatch_watch_registration_guarded()
            _run_pr_state_hydration_guarded(config)
            _run_stale_gate_release_guarded()
            _check_version_drift()
            result = dispatch_tick(
                config,
                use_plan=use_plan,
                parent=parent,
                native_daemon=resolved_native_daemon,
                emit=emit,
                warned_stale=warned_stale,
                warned_fetch_fail=warned_fetch_fail,
                warned_collision=warned_collision,
                warned_ssh_key=warned_ssh_key,
                warned_disk_pressure=warned_disk_pressure,
                usage_limited_until=usage_limited_until,
                auto_ff=auto_ff,
                client_filter=client,
            )

            if result.usage_limit_detected and not once:
                usage_limited_until = datetime.now(UTC) + timedelta(
                    seconds=config.usage_limit_backoff_seconds
                )
                save_usage_limited_until(usage_limited_until)
                # #1343 R2: stamp the arm timestamp on every fresh detection
                # (this block already only fires when the window was NOT
                # already active this tick -- see tick.py's early-return at
                # usage_limit_detected=False while a window is active) so
                # the eventual cleared-event's detected_at is exact, not
                # derived from backoff_seconds.
                usage_limit_window_armed_at = datetime.now(UTC)
                save_usage_limit_armed_at(usage_limit_window_armed_at)
                _log.warning(
                    "dispatch: usage limit detected; backing off until %s",
                    usage_limited_until,
                )

            if once:
                return

            time.sleep(config.tick_interval_seconds)
    finally:
        # Bounded drain of in-flight codex review threads (#1727). Deliberately
        # unconditional rather than gated on a clean exit: every path that
        # reaches this block at all is, by definition, not a SIGKILL, so Python
        # is still alive to give an almost-done review a few seconds to land —
        # and an exception unwinding the loop is exactly as likely to strand a
        # half-committed worktree as a clean shutdown is.
        #
        # Suppressed for the same reason the event write below is: shutdown
        # bookkeeping must never replace the exception that is already unwinding
        # this frame. A failed drain leaves the count at its 0 default rather
        # than swallowing the real cause of the exit.
        _codex_threads_still_running = 0
        with contextlib.suppress(Exception):
            _codex_threads_still_running = join_outstanding_codex_threads()
        _exc = sys.exc_info()[1]
        _normal = _exc is None or isinstance(_exc, KeyboardInterrupt)
        _payload: dict[str, object] = {
            "normal": _normal,
            "exception_type": None if _normal else type(_exc).__name__,
            # Free observability for #1742: distinguishes "the loop exited" from
            # "the loop exited while N reviews were still running".
            "codex_threads_still_running": _codex_threads_still_running,
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
