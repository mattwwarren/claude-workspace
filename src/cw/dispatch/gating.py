"""Availability, freshness, and usage-limit gating for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the preflight gates that
decide whether a client is eligible to dispatch this tick."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
)
from cw.dispatch_state import (
    AvailabilityProbeCache,
    load_availability_probe_cache,
    save_availability_probe_cache,
)
from cw.events import record_event
from cw.exceptions import (
    MissingWorkspaceError,
    WorktreeError,
)
from cw.executor import resolve_executor_config
from cw.gh import check_gh_availability
from cw.models import (
    CONTEXT_JSON_RELATIVE_PATH,
    LOCAL_BACKEND,
    ClientConfig,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile import (
    reconcile,
)
from cw.worktree import (
    check_main_ff_safety,
    fast_forward_main,
    get_head_branch,
    is_main_behind_origin,
    is_main_checkout_dirty,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
    )
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger("cw.dispatch")


# paused_status written to SESSION_NEEDS_ATTENTION when the fleet-wide
# gh-availability latch trips (RFC 0011 A5). Deliberately distinct from
# _AWAITING_OPERATOR_REASON, which is scoped (see its own comment above) to
# Rule 5's per-task blocked status with session_id/ticket_id populated
# (RFC 0011 A1) -- an unrelated, per-task event shape. This constant's event
# is fleet-wide and sessionless (session_id="", client="", ticket_id=None).
# See GitHub #1157.
_AVAILABILITY_OUTAGE_REASON = "gh_availability_outage"


# Timeout (seconds) for the fleet-wide `gh auth status` availability probe.
# Value mirrors cw.operator_identity._GH_LOGIN_TIMEOUT_SECONDS (also 10); not
# imported directly because that would invert the circular-import direction.
_AVAILABILITY_PROBE_TIMEOUT_SECONDS = 10


# TTL (seconds) for the fleet-wide availability probe cache. Within this
# window a tick reuses the cached verdict instead of re-shelling `gh auth
# status`, so a multi-client fleet pays at most one probe per TTL, not one per
# client per tick.
_AVAILABILITY_PROBE_TTL_SECONDS = 60


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
        backoff_lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
        backoff_lane_stats = _lane_stats_for_client(
            client, queue_snapshot, occupants=backoff_lane_occupants
        )
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
                "lane_occupants": backoff_lane_occupants,
                "occupied": sum(len(v) for v in backoff_lane_occupants.values()),
            },
        )


FRESHNESS_NON_MAIN_HEAD = "non_main_head"


FRESHNESS_MAIN_BEHIND = "main_behind_origin"


FRESHNESS_MAIN_DIRTY_CHECKOUT = "main_dirty_checkout"


FRESHNESS_MAIN_DIVERGED = "main_diverged_from_origin"


FRESHNESS_MAIN_DETACHED = "main_detached_head"


def _resolve_availability() -> bool:
    """Fleet-wide TTL-cached gh-availability probe (RFC 0011 A5).

    Mirrors :func:`_resolve_freshness`'s check-and-cache shape but fleet-wide,
    not per-client: state lives in dispatch_state.py's DISPATCH_STATE_FILE sidecar
    (:class:`~cw.dispatch_state.AvailabilityProbeCache`), not a ConcurrencyOverrides
    client entry. On a cache hit (probed within
    ``_AVAILABILITY_PROBE_TTL_SECONDS``) returns the cached verdict without
    calling gh or touching the latch. On a cache miss, calls
    :func:`~cw.gh.check_gh_availability`, persists the fresh verdict, and
    updates the fleet-wide latch: :func:`_record_availability_block` on a
    fresh failure (fires SESSION_NEEDS_ATTENTION once per outage episode --
    edge-triggered), :func:`_reset_availability_block` on a fresh success. On
    any resolution error, fails open -- same posture as _resolve_freshness's
    own ``except Exception`` fail-open.
    """
    try:
        cache = load_availability_probe_cache()
        now = datetime.now(UTC)
        if (
            cache is not None
            and (now - cache.probed_at).total_seconds()
            < _AVAILABILITY_PROBE_TTL_SECONDS
        ):
            return cache.available

        available = check_gh_availability(timeout=_AVAILABILITY_PROBE_TIMEOUT_SECONDS)
        was_latched = cache.latched if cache is not None else False
        if available:
            _reset_availability_block(now=now)
        else:
            _record_availability_block(now=now, was_latched=was_latched)
    except Exception:  # noqa: BLE001
        # Defense-in-depth: check_gh_availability already fails closed on its
        # own subprocess errors; this catches an error resolving the cache or
        # persisting the verdict. Fail open so a transient sidecar issue never
        # blocks the whole loop, mirroring _resolve_freshness's fail-open.
        _log.warning("dispatch_tick: availability probe failed to resolve; proceeding")
        return True
    return available


def _resolve_availability_once(available: bool | None) -> bool:
    """Return *available* unchanged, or resolve it via :func:`_resolve_availability`.

    Per-tick memoization helper for :func:`dispatch_tick`'s client loop:
    ``available`` starts ``None`` each tick and is only ever resolved once,
    the first time the loop body runs for a client (not hoisted above the
    loop, so an empty ``clients`` dict or a fully-paused fleet never probes).
    Extracted to keep the branch this adds out of ``dispatch_tick`` itself
    (PLR0912).
    """
    return _resolve_availability() if available is None else available


def _record_availability_block(*, now: datetime, was_latched: bool) -> None:
    """Persist a fresh failed probe and fire attention once per outage episode.

    Writes the AvailabilityProbeCache with ``latched=True`` and, when the
    fleet was not already latched (edge-triggered), emits a single fleet-wide
    ``session.needs_attention``. Sibling of
    :func:`_record_client_freshness_block`, but the latch lives in the probe
    cache (fleet-wide) rather than a per-client override counter. The event is
    sessionless and clientless (``session_id=""``, ``client=""``): a
    fleet-wide outage has no single node to name. No push notification
    (deliberate -- matches the freshness-block escalation precedent).

    Why edge-triggered, not debounce-N: this fleet latch fires on the FIRST
    bad probe, diverging from its three sibling latches
    (``freshness_block_attention_threshold``, ``salvage_skip_attention_threshold``,
    ``lane_circuit_breaker_threshold``), which all debounce N>=2 observations
    before paging. Those three exist to filter single-node/single-lane
    transient noise; a fleet-wide `gh auth status` failure has no analogous
    per-node noise source (every client observes the identical outage
    simultaneously), so debouncing would only delay the operator's first
    signal of an already-fleet-wide condition. See RFC 0011 A5 / #1157.
    """
    save_availability_probe_cache(
        AvailabilityProbeCache(probed_at=now, available=False, latched=True)
    )
    if not was_latched:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "",
                "session_name": "",
                "client": "",
                "ticket_id": None,
                "claude_session_id": None,
                "paused_status": _AVAILABILITY_OUTAGE_REASON,
                "breadcrumbs": "availability_probe_failed",
                "crashed": False,
            },
            correlation_id=None,
        )


def _reset_availability_block(*, now: datetime) -> None:
    """Persist a fresh successful probe, clearing the fleet-wide outage latch.

    Edge-triggered reset: writes the AvailabilityProbeCache with
    ``latched=False`` so the next outage episode re-fires attention. Sibling
    of :func:`_reset_client_freshness_blocks`; unlike that helper it always
    writes, because the fresh ``probed_at`` also refreshes the TTL window.
    """
    save_availability_probe_cache(
        AvailabilityProbeCache(probed_at=now, available=True, latched=False)
    )


def _emit_availability_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
) -> None:
    """Emit a dispatch.tick skip event for an availability-gated client.

    Mirrors :func:`_emit_stale_skip`'s dispatch.tick emission (same
    ``pending``/``running``/``cap``/``lanes`` fields, ``claimed=0``) minus its
    freshness-specific TICKET_NEEDS_SYNC + resync-WARN loop -- a fleet-wide gh
    outage is not a per-ticket sync problem. ``skip_reason`` is
    ``AVAILABILITY_GATE``.
    """
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.AVAILABILITY_GATE,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )


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
        if ff_safety == "detached":
            return (True, FRESHNESS_MAIN_DETACHED)
        # "ahead" is theoretically unreachable here: stale=True requires
        # is_main_behind_origin to return behind_count>0, which means local
        # is behind origin — not ahead. The guard is kept for defensive
        # completeness (worktree.py:check_main_ff_safety documents this).
        if ff_safety in ("ahead", "diverged"):
            return (True, FRESHNESS_MAIN_DIVERGED)
        if ff_safety == "behind" and is_main_checkout_dirty(client):
            return (True, FRESHNESS_MAIN_DIRTY_CHECKOUT)
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
    provided value (``"non_main_head"``, ``"main_behind_origin"``,
    ``"main_dirty_checkout"``, ``"main_diverged_from_origin"``, or
    ``"main_detached_head"``).
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
                elif freshness_detail == FRESHNESS_MAIN_DIRTY_CHECKOUT:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main checkout has uncommitted changes, ticket skipped"
                        f" — commit or stash changes in {client.workspace_path}"
                    )
                elif freshness_detail == FRESHNESS_MAIN_DETACHED:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main checkout has a detached HEAD, ticket skipped"
                        f" — run: git -C {client.workspace_path}"
                        f" checkout {client.default_branch}"
                    )
                elif freshness_detail == FRESHNESS_MAIN_DIVERGED:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main has diverged from origin, ticket skipped"
                        " — inspect before touching it: git -C"
                        f" {client.workspace_path} log origin/"
                        f"{client.default_branch}..HEAD --oneline —"
                        " do NOT auto-rebase or reset; stray commits"
                        " may need manual triage"
                    )
                else:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main behind origin, ticket skipped"
                    )
                if warned_stale is not None:
                    warned_stale.add(ticket_key)
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
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
            "blocked_branch": non_main_branch,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )


def _invalidate_stale_context_json(
    task: TicketTask, client: ClientConfig, worktree_path: Path
) -> None:
    """Delete a stale ``.cw/context.json`` before spawning a re-spawned task.

    Requeue idempotency guard (#1046): a re-spawned task (``attempts > 1``,
    covering true requeues as well as normal plan->impl->review stage
    advances) may reuse a worktree that still carries a prior session's
    materialized ``.cw/context.json``. Left in place, a worker can silently
    replan against stale ticket context and miss operator-folded
    resolutions/comments (the #1030 incident). Delete it before spawn so the
    new session always materializes fresh context.

    Excluded for LocalExecutor: ``local_runner.build_task_message`` reads
    ``.cw/context.json`` directly and degrades silently to an empty
    ``## Ticket:`` header if it is missing (the #952 regression class).
    """
    if task.attempts <= 1:
        return
    if resolve_executor_config(task.stage, task, client).backend == LOCAL_BACKEND:
        return
    stale_context = worktree_path / CONTEXT_JSON_RELATIVE_PATH
    if stale_context.exists():
        _log.info(
            "dispatch: invalidated stale .cw/context.json for"
            " ticket_id=%s attempts=%d worktree_path=%s",
            task.ticket_id,
            task.attempts,
            worktree_path,
        )
    stale_context.unlink(missing_ok=True)


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
