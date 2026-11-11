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
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
)
from cw.dev_queue.review_debt_enqueue import enqueue_review_debt_for_merged_prs
from cw.dispatch_loop_identity import loop_record_fields
from cw.dispatch_state import (
    clear_all_executor_blocked_markers,
    load_usage_limit_armed_at,
    load_usage_limited_until,
)
from cw.events import advance_cursor, read_events, record_event
from cw.exceptions import (
    ConfigValidationError,
    CwError,
    CwLockOrderError,
    CwLockReentrancyError,
    VersionDriftError,
)
from cw.models import OrchestratorEventType
from cw.native_daemon import get_native_daemon_client
from cw.orchestrate import latest_tick_summary_by_client
from cw.pr_hydrate import hydrate_pr_states
from cw.reconcile import (
    _CAUSE_USAGE_LIMIT,
    register_stale_dispatch_watched_prs,
    release_stale_gated_tasks,
)
from cw.reconcile.codex_boot import reap_orphaned_codex_sessions_at_boot
from cw.review_debt_filing import CloseOutcome, DebtCloseState, sweep_debt_closes
from cw.session_auto_prune import auto_prune_sessions
from cw.shipped_pr_signal import scan_shipped_red_prs
from cw.sprint import GhClient
from cw.worker_tmp_retention import auto_sweep_fallback_tmp
from cw.worktree import TickFetchGate, _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cw.models import (
        ClientConfig,
        OrchestratorConfig,
        OrchestratorEvent,
    )
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree import FetchWarningKey, UnresolvablePathWarningKey
from cw.dispatch.branch_freshness import (
    BranchKey,
    precomputed_branch_conflicts,
    resolve_conflicting_branches,
)
from cw.dispatch.lanes import _notify_stale_clients_with_pending

# Moved to flat siblings (#2999) and imported back: ``loop``'s own callers use
# them, the suite still imports them from this path, and none of them reads a
# name the suite patches on ``cw.dispatch.loop``.
from cw.dispatch.session_completions import _apply_events_to_store
from cw.dispatch.tick import dispatch_tick
from cw.dispatch.usage_limit_windows import (
    _arm_and_persist_usage_limit_window,
    _merge_persisted_usage_limited_until,
    _usage_limit_window_is_active,
)

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


def consume_completed_sessions() -> int:
    """Process session.completed events from the dispatch inbox.

    Reads new SESSION_COMPLETED events from the inbox since the last cursor
    position for the "dispatch" consumer. For each event that carries a
    ``ticket_id`` in its payload, the corresponding TicketTask (if found in
    RUNNING state) is marked COMPLETED.

    Advances the cursor after processing. A composition of the lock-free
    read half and the apply half (#2692), which the CLI's row-lag settle
    also uses under its own bounded lock.

    Before the lock, the REVIEW ``branch_behind_main`` gate's merge probes run
    (#2979, :func:`~cw.dispatch.branch_freshness.resolve_conflicting_branches`)
    against a lock-free queue snapshot, and their verdicts are installed only
    for the locked section: no git call runs under ``dev_queue_lock`` for that gate
    (the pr_gate precompute pattern; ADR-0022 invariant 3); no verdict outlives it.

    Returns:
        Number of TicketTasks transitioned to COMPLETED.
    """
    events = read_pending_completions()
    if not events:
        return 0
    conflicts = _conflicting_branches_lock_free(events)
    with dev_queue_lock(), precomputed_branch_conflicts(conflicts):
        return apply_pending_completions(events)


def _conflicting_branches_lock_free(
    events: list[OrchestratorEvent],
) -> frozenset[BranchKey]:
    """The REVIEW gate's conflict verdicts for *events*, read without the lock.

    An unreadable queue or client config (``OSError``, ``ValueError``,
    ``CwError``) yields no verdicts: the gate fails open, and the locked apply
    surfaces the error on its own read if it persists.
    """
    try:
        store = load_dev_queue()
        clients = load_effective_clients()
    except (OSError, ValueError, CwError):
        _log.warning(
            "branch_freshness: could not read the queue for the REVIEW branch "
            "probe; no branch parks this tick",
            exc_info=True,
        )
        return frozenset()
    return resolve_conflicting_branches(events, store, clients)


def read_pending_completions() -> list[OrchestratorEvent]:
    """The unconsumed SESSION_COMPLETED events for the dispatch cursor.

    Read-only: takes no dev-queue lock and moves no cursor (#2692).
    """
    return read_events(
        consumer=_DISPATCH_CONSUMER,
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )


def apply_pending_completions(events: list[OrchestratorEvent]) -> int:
    """Route *events* (non-empty) and advance the dispatch cursor past them.

    The caller holds ``dev_queue_lock``. Returns the number of TicketTasks
    routed out of RUNNING.
    """
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
    completed = _apply_pending_completion_events(events)
    # Advance cursor inside the dev-queue lock so the cursor never
    # moves past events whose queue mutations haven't been persisted yet.
    advance_cursor(_DISPATCH_CONSUMER, events[-1].id)
    return completed


def _apply_pending_completion_events(events: list[OrchestratorEvent]) -> int:
    """Load routing state and apply pending completion *events*."""
    store = load_dev_queue()
    clients = load_effective_clients()
    return _apply_events_to_store(store, events, clients=clients)


def apply_pending_completion(event: OrchestratorEvent) -> int:
    """Route one pending completion without moving the shared cursor.

    The CLI row-lag settle uses this when the requested event is behind other
    tenants' events in the dispatch inbox.  The normal consumer still drains
    the cursor in order later; this targeted path must not consume or route
    the unrelated prefix merely because one operator asked about this row.
    Caller must hold ``dev_queue_lock``.
    """
    return _apply_pending_completion_events([event])


TICK_STALE_SECONDS = 90  # 3x default tick_interval_seconds=30


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
    usage_limited_until: Mapping[str, datetime],
    armed_at: datetime | None,
) -> bool:
    """Detect an armed->cleared usage-limit backoff transition; emit for it (#1343).

    Returns this tick's active state, for the caller to carry forward as
    ``was_active`` into the next tick's call. Fires
    :attr:`OrchestratorEventType.USAGE_LIMIT_CLEARED` exactly once -- on the
    tick that observes ``was_active and not now_active`` -- never per-client
    (a single event carries the full ``clients_affected`` cohort), so
    "cleared" means the LAST client's window lapsed even though the windows
    themselves are per-client since #1409. The set side is audited per client
    by :attr:`OrchestratorEventType.USAGE_LIMIT_ARMED` instead.
    ``usage_limited_until`` and ``armed_at`` stay keyword-only: a positional
    call site could otherwise swap them silently past mypy. "Active" mirrors
    the wall-clock check :func:`~cw.dispatch.tick.dispatch_tick` itself uses
    to gate spawning (a client's entry present AND still in the future) --
    an entry is never removed on natural expiry (only overwritten by a fresh
    detection), so a presence check alone would never observe a transition.

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
        # #2729: record the code this process loaded, read once per process.
        lock_cm = dispatch_loop_lock(identity=loop_record_fields())
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


def _run_shipped_red_scan_guarded(config: OrchestratorConfig) -> None:
    """Run the red-shipped-PR sweep (#2945) as a non-fatal tick step.

    Returns at once when ``shipped_red_signal_enabled`` is False (the global
    off-switch, re-read each tick). Sibling of
    ``_run_pr_state_hydration_guarded`` with the same sanctioned broad catch.
    """
    if not config.shipped_red_signal_enabled:
        return
    try:
        scan_shipped_red_prs(config)
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. scan_shipped_red_prs reads ``gh`` and writes queue rows and
        #    events -- failure modes include subprocess crash, JSON decode,
        #    lock/queue-file/event-inbox I/O and malformed persisted rows.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical: the sweep is best-effort signalling; skipping a
        #    pass only delays the page, it must never wedge the dispatch loop.
        # 4. Paired test: tests/test_dispatch.py
        #    TestRunDispatchLoopHydrationHook.
        _log.exception("red-shipped-PR sweep failed during tick; continuing")


#: Process-local close-sweep state (#3044): the interval gate and its memos.
#: Module-level rather than a ``_run_dispatch_loop_body`` local because that
#: function sits at the PLR0915 statement cap; it is passed explicitly, so the
#: library stays pure and tests use a fresh :class:`DebtCloseState`.
_DEBT_CLOSE_STATE = DebtCloseState()


def _debt_tracker_for(client: ClientConfig) -> GhClient:
    """The close sweep's per-client tracker, scoped to the client's repo."""
    return GhClient(cwd=_git_dir(client))


def _run_debt_close_sweep_guarded(
    config: OrchestratorConfig, state: DebtCloseState
) -> None:
    """Run the review-debt close sweep (#3044) as a non-fatal tick step.

    Returns at once when ``review_debt_close_enabled`` is False (re-read each
    tick) or when *state*'s interval gate is not due. The gate advances before
    the sweep runs, so a sweep that raises still waits a full interval rather
    than retrying every tick. Lock-discipline errors are programming errors
    and are re-raised; every operational failure is logged and the loop keeps
    ticking, since a missed sweep only delays a close to the next one. Paired
    test: tests/test_dispatch_debt_close_hook.py.
    """
    if not config.review_debt_close_enabled or not state.claim():
        return
    try:
        results = sweep_debt_closes(
            load_clients(), state, tracker_for=_debt_tracker_for
        )
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. sweep_debt_closes reads ``gh`` and closes issues -- failure modes
        #    include subprocess crashes, malformed responses and unexpected
        #    third-party/config errors beyond the known exception tuple.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical: skipping a pass only delays a close; it must never
        #    wedge the dispatch loop.
        # 4. Paired test: tests/test_dispatch_debt_close_hook.py covers an
        #    unexpected RuntimeError and verifies the gate still advances.
        _log.exception("review-debt close sweep failed during tick; continuing")
        return
    closed = sum(1 for result in results if result.outcome is CloseOutcome.CLOSED)
    if closed:
        _log.info("review-debt close sweep closed %d debt ticket(s)", closed)


def _run_pr_state_steps_guarded(config: OrchestratorConfig) -> None:
    """PR-state hydration, the red-shipped-PR sweep, then the debt close sweep.

    Independent guards: a raising hydration does not stop either sweep from
    being attempted, and a raising sweep never stops the tick. One call site
    keeps ``_run_dispatch_loop_body`` under PLR0915.
    """
    _run_pr_state_hydration_guarded(config)
    _run_shipped_red_scan_guarded(config)
    _run_debt_close_sweep_guarded(config, _DEBT_CLOSE_STATE)


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
        #    rescan — the next tick re-derives whatever candidates remain
        #    unregistered from current on-disk state.
        # 4. Paired test: tests/test_dispatch.py
        #    TestRunDispatchLoopStaleDispatchWatchHook.
        _log.exception(
            "stale-dispatch watch registration failed during tick; continuing"
        )


def _run_pr_merge_consumers_guarded() -> None:
    """Run the consumers of ``PR_MERGED``: stale-gate release, then debt auto-enqueue.

    Folded into one helper so ``_run_dispatch_loop_body`` stays inside the
    PLR0915 statement budget (#3045). The debt enqueue runs straight after the
    stale-gate release and before ``dispatch_tick``, so a freshly enqueued debt
    row is dispatchable in the same tick.
    """
    _run_stale_gate_release_guarded()
    _run_review_debt_enqueue_guarded()


def _run_review_debt_enqueue_guarded() -> None:
    """Run the review-debt enqueue, swallowing operational failures (#3045).

    Lock-discipline errors (``CwLockReentrancyError``, ``CwLockOrderError``) are
    programming errors and are re-raised. Anything else is logged and the tick
    continues. Safe to retry: a failed event's cursor is not advanced, and each
    candidate add is an existing-row check, so a replay never mints a sibling.
    """
    try:
        enqueue_review_debt_for_merged_prs()
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except (CwError, OSError, ValueError):
        _log.exception("review-debt enqueue failed during tick; continuing")


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


def _run_stale_client_watchdog_guarded(
    config: OrchestratorConfig, next_scan_at: datetime | None
) -> datetime | None:
    """Run the stale-client watchdog, at most once per gate interval (#1875).

    Returns the instant the next scan becomes eligible; the caller threads it
    back in on the following tick. Fourth member of the guarded-tick-step
    family (``_run_pr_state_hydration_guarded`` et al) but the only one
    carrying a call-frequency gate, because
    :func:`~cw.dispatch.lanes._notify_stale_clients_with_pending` calls
    ``latest_tick_summary_by_client()``, which reads and parses the ENTIRE
    events inbox on every call -- ``cw.events.read_events``'s
    ``since_ts``/``event_types``/``limit`` arguments filter the parsed result,
    not the read/parse cost (see ``src/cw/events.py``). At the default 30s
    tick cadence against an unrotated multi-tens-of-MB inbox that is real
    hot-loop cost.

    The gate is process-lifetime local state threaded through the loop body,
    the same shape as its ``warned_stale``/``warned_fetch_fail`` siblings --
    NOT a ``since_ts`` window, which would fail twice over: it would not
    reduce ``read_events``' cost (above), and it would actively break
    detection, because a client stale for longer than the window has zero
    DISPATCH_TICK events in range and simply vanishes from
    ``latest_tick_summary_by_client()``'s result -- silently suppressing the
    long-stale case this ticket is about.

    The gate advances even when the call raises, so a persistently-failing
    watchdog cannot wedge the loop into retrying it every single tick.
    """
    now = datetime.now(UTC)
    if next_scan_at is not None and now < next_scan_at:
        return next_scan_at
    try:
        _notify_stale_clients_with_pending(
            stale_after_seconds=TICK_STALE_SECONDS,
            notify_interval_minutes=config.dispatch_stale_notify_interval_minutes,
        )
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. The watchdog reads the events inbox and the executor-blocked
        #    marker sidecar, writes the concurrency-override store under a
        #    lock, and emits an event — failure modes include I/O, lock
        #    contention, and JSON/payload surprises.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical: a missed staleness page only delays operator
        #    notice; the next scan re-derives the identical stale set from
        #    current on-disk state, and the loop must keep ticking regardless.
        # 4. Paired test: tests/test_dispatch.py
        #    TestRunDispatchLoopStaleClientWatchdogHook.
        _log.exception("stale-client watchdog failed during tick; continuing")
    return now + timedelta(minutes=config.dispatch_stale_notify_interval_minutes)


def _run_sessions_auto_prune_guarded(config: OrchestratorConfig) -> None:
    """Run the automatic sessions prune, which gates itself on its own interval (#2965).

    Called after ``dispatch_tick`` returns, where the loop thread holds no
    guarded lock; ``prune_sessions`` takes one bounded ``sessions_lock``, so a
    caller already holding it would raise ``CwLockReentrancyError``. That
    error and ``CwLockOrderError`` are lock-discipline violations -- programming
    errors, not operational failures -- and are re-raised, never swallowed by
    the ``CwError`` clause below. Everything else the prune can raise
    (``CwError``, ``OSError``, ``ValueError``) is logged and the loop keeps
    ticking: ``auto_prune_sessions`` already records an operational failure in
    its sidecar, and a missed prune only delays the next one.
    """
    try:
        auto_prune_sessions(config)
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except (CwError, OSError, ValueError):
        _log.exception("sessions auto-prune failed during tick; continuing")


def _run_worker_tmp_retention_guarded(config: OrchestratorConfig) -> None:
    """Run the ``~/.cw/tmp`` fallback retention sweep, self-gated (#2682).

    Same contract as :func:`_run_sessions_auto_prune_guarded`: called with no
    guarded lock held, lock-discipline errors are re-raised, and any operational
    failure is logged so the loop keeps ticking. The sweep records its own
    failure in the sidecar, so a missed run only delays the next one.
    """
    try:
        auto_sweep_fallback_tmp(config)
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except (CwError, OSError, ValueError):
        _log.exception("fallback tmp retention failed during tick; continuing")


def _run_post_tick_steps_guarded(
    config: OrchestratorConfig, stale_watchdog_next_scan_at: datetime | None
) -> datetime | None:
    """Run the guarded steps that follow ``dispatch_tick``; return the watchdog gate.

    Folded into one helper so ``_run_dispatch_loop_body`` stays inside the
    PLR0915 statement budget (#2965). Order is the stale-client watchdog
    (#2076: it must scan after the tick records its events), then the sessions
    auto-prune and the fallback tmp retention (#2682), which need no particular
    position beyond running with no guarded lock held.
    """
    next_scan_at = _run_stale_client_watchdog_guarded(
        config, stale_watchdog_next_scan_at
    )
    _run_sessions_auto_prune_guarded(config)
    _run_worker_tmp_retention_guarded(config)
    return next_scan_at


def _warn_if_scoped_serve_starves_siblings(scoped_client: str | None) -> None:
    """WARN once at boot when a scoped ``serve`` strands other clients (#1875).

    ``dispatch_tick``'s ``client_filter`` narrowing means a
    ``cw dev-queue serve -c X`` process never ticks any client but X, while
    the #1362 singleton lock stops a second loop from covering the rest — so
    every other client's queue silently starves for as long as this process
    runs. Log-only, mirroring :func:`run_dispatch_loop`'s ``--force`` bypass
    WARNING: the durable, machine-actionable signal is the watchdog's
    recurring ``dispatch_loop_stale`` attention event, which this line only
    front-runs so an operator watching stdout learns immediately rather than
    after the first staleness interval.

    Single-shot at process start, so deliberately NOT behind
    :func:`_run_stale_client_watchdog_guarded`'s scan gate — one inbox read
    per boot is not the per-tick cost that gate exists to bound.
    """
    if scoped_client is None:
        return
    tick_data = latest_tick_summary_by_client()
    starved = sorted(
        client
        for client, tick in tick_data.items()
        if client != scoped_client and tick.pending > 0
    )
    if starved:
        _log.warning(
            "dispatch: scoped serve (client=%s) holds the #1362 singleton lock"
            " while %s have pending work and will not be dispatched until this"
            " process exits or is rescoped",
            scoped_client,
            ", ".join(starved),
        )


def _run_boot_passes(client: str | None) -> None:
    """Run the once-per-process passes that precede the first tick.

    Extracted from :func:`_run_dispatch_loop_body` (PLR0915) when #1875 added
    the third of them. All three run under the singleton lock (#1362), before
    any tick, and are independent of one another:

    * the orphaned-codex reap (#1727) — a codex session still ACTIVE at
      process start means the prior process died mid-review (crash/SIGKILL),
      the one case the loop's shutdown join cannot reach since that thread
      died with its process. Each orphan's session is closed and its task is
      requeued when provably clean under ``reap_policy: auto``, or parked for
      operator inspection otherwise (#2285);
    * the executor-blocked marker clear (#1742) — any marker still on disk is
      orphaned, because no daemon thread survives a process restart. Kept
      separate from the reap above on purpose: disposing of an orphaned
      session and clearing a stale marker are different concerns;
    * the scoped-serve starvation WARNING (#1875).
    """
    orphaned_codex = reap_orphaned_codex_sessions_at_boot()
    if orphaned_codex:
        _log.warning(
            "dispatch: %d codex session(s) were ACTIVE at process start;"
            " each was closed and requeued if provably clean, otherwise parked"
            " for operator inspection",
            orphaned_codex,
        )
    clear_all_executor_blocked_markers()
    # An operator typing `serve -c X` into an already-loaded fleet gets told
    # immediately, not one staleness interval later.
    _warn_if_scoped_serve_starves_siblings(client)


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
    _run_boot_passes(client)

    resolved_native_daemon = native_daemon or get_native_daemon_client()
    # Track stale-warn deduplication across all ticks within this run.
    warned_stale: set[tuple[str, str]] = set()
    # Track fetch-fail-warn deduplication for persistently unreachable remotes,
    # keyed on the failure (client, outcome, reason) so a new failure still warns.
    warned_fetch_fail: set[FetchWarningKey] = set()
    # Bound on the tick's read-only network git (#2910); loop-scoped like its
    # warned_* siblings, sharing warned_fetch_fail, so a client's fetch-timeout
    # backoff survives from one tick to the next.
    fetch_gate = TickFetchGate(warned=warned_fetch_fail)
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
    # Track unresolvable-record-warn deduplication across all ticks within
    # this run; keyed on (kind, path, error) so a session-side and worker-side
    # break of the same path warn independently -- see
    # cw.worktree.UnresolvablePathWarningKey.
    warned_unresolvable: set[UnresolvablePathWarningKey] = set()
    # Sentinel parse-warning dedup across ticks (#1394): process-lifetime and
    # bounded in place by _reconcile_usage_limited (WARNED_BLOCKS_MAX_KEYS).
    warned_blocks: set[str] = set()
    # Scan-frequency gate for the cross-client staleness watchdog (#1875);
    # process-lifetime local like the warn-dedup sets above. See
    # _run_stale_client_watchdog_guarded for why the gate is a call-frequency
    # bound rather than a read-window narrowing.
    stale_watchdog_next_scan_at: datetime | None = None
    # Back-off window: loaded from the persisted sidecar so a loop restart after
    # a code merge honours an active backoff rather than re-opening the spawn gate
    # immediately (#804).
    # Keyed by client name since #1409 -- one client's parsed reset must not
    # park the rest of the fleet.
    usage_limited_until: dict[str, datetime] = load_usage_limited_until()
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
            _run_pr_state_steps_guarded(config)
            _run_pr_merge_consumers_guarded()
            _check_version_drift()
            with fetch_gate.tick():
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
                    warned_unresolvable=warned_unresolvable,
                    usage_limited_until=usage_limited_until,
                    auto_ff=auto_ff,
                    client_filter=client,
                    warned_blocks=warned_blocks,
                )

            # AFTER dispatch_tick, not before it (#2076): the scan reads the
            # latest recorded dispatch.tick per client, so scanning first
            # meant a healthy loop's newest tick was always one full
            # iteration old at scan time — sleep + the guarded pre-tick
            # passes (PR hydration's gh calls included) + the tick's own
            # spawn work routinely exceeds TICK_STALE_SECONDS. Post-tick,
            # a healthy loop's newest tick is seconds old when scanned.
            stale_watchdog_next_scan_at = _run_post_tick_steps_guarded(
                config, stale_watchdog_next_scan_at
            )

            if result.usage_limit_clients and not once:
                usage_limited_until, usage_limit_window_armed_at = (
                    _arm_and_persist_usage_limit_window(
                        usage_limited_until,
                        result.usage_limit_clients,
                        backoff_seconds=config.usage_limit_backoff_seconds,
                    )
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
