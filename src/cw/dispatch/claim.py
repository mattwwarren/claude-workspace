"""Task claim + spawn primitives for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the atomic claim step, the
per-lane occupant/stat snapshots, and the worktree-provision + spawn path."""

from __future__ import annotations

import contextlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import (
    StaleWorktreeError,
    UsageLimitError,
    WorktreeError,
)
from cw.executor import (
    CodexCapabilityDiagnosis,
    codex_capability_diagnosis,
    resolve_executor,
    resolve_executor_config,
)
from cw.models import (
    CODEX_BACKEND,
    OCCUPIED_LANE_STATUSES,
    ClientConfig,
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile import (
    resolve_headless_budget,
)
from cw.worktree import (
    check_not_main_checkout,
    create_worktree,
    remove_worktree,
    worktree_has_unsaved_work,
    worktree_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger("cw.dispatch")


_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS: int = 2


_SPAWN_ERROR_BACKOFF_CAP_SECONDS: int = 300

# TTL (seconds) for the in-process codex-capability probe cache (#1238). Codex
# CLI presence/version essentially never changes between dispatch ticks, so a
# short process-lifetime cache avoids re-shelling `codex --version` on every
# codex-backed spawn attempt. Unlike gating.py's _AVAILABILITY_PROBE_TTL_SECONDS
# this has no fleet-wide sidecar persistence or latch semantics -- it's a
# per-task gate, not a fleet-wide outage signal, so a plain in-memory cache is
# sufficient.
_CODEX_CAPABILITY_PROBE_TTL_SECONDS = 60

# Timeout for the codex-capability probe's own `codex --version` subprocess
# call when invoked from this hot path (#1238). `_spawn_claimed_task` runs
# synchronously inside dispatch_tick's per-client, per-lane loop, so a stuck
# `codex` binary would otherwise stall that entire tick for up to
# executor.py's one-shot-appropriate 10s default; use a much smaller budget
# here since `codex --version` is a trivial local command expected to return
# near-instantly on a healthy install.
_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS = 3

# Consecutive codex-capability parks (across any client/task sharing this
# process) tolerated before the gate also raises the generic `spawn_error`
# signal, as a bounded backstop (#1238). Below this count, a park stays
# decoupled from the per-lane circuit breaker (see _codex_capability_gate's
# docstring — an isolated park must not durably pause an unrelated lane).
# At/above it, the condition has stopped looking like an isolated blip and
# started looking systemic (e.g. a wrong probe verdict, per the TTL-cached
# result being shared across every codex-backed task in the process), so the
# existing circuit-breaker/operator-visible-pause machinery is allowed to
# engage rather than letting every codex-backed task in the queue drain into
# BLOCKED_ON_USER with no self-limiting mechanism at all.
_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD = 3

# In-process cache for the codex-capability probe (#1238). A single-element
# list is used as a mutable slot -- updates mutate its contents in place
# rather than rebinding the module-level name, so no `global` statement (and
# no PLW0603 suppression) is needed. Populated lazily by
# _cached_codex_capability_diagnosis; reset via _reset_codex_capability_cache
# (test support only -- production code never needs to invalidate early since
# codex CLI presence/version doesn't change mid-process).
_codex_capability_cache: list[tuple[CodexCapabilityDiagnosis, datetime]] = []

# Consecutive-park counter backing _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD
# (#1238). Same mutable-single-element-list-as-slot idiom as the cache above.
# Incremented on every park; reset to 0 as soon as the probe reports capable
# again (see _codex_capability_gate), so this genuinely tracks a *consecutive*
# streak of parks, not a lifetime total -- a long-lived dispatch-loop process
# that recovers between incidents must not have old, unrelated parks silently
# combine with a later isolated one to trip the circuit breaker.
_codex_capability_park_count: list[int] = [0]


def _emit_attempt_cap_blocked_event(client_name: str, ticket_id: str) -> None:
    """Emit a dispatch.tick event when a task is parked at the global attempt ceiling.

    Per-task event (not per-client-per-tick) for operator observability: a quiet
    loop after several failures should be distinguishable from a healthy idle loop.
    See GitHub #786.
    """
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client_name,
            "claimed": 0,
            "skip_reason": DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
            "ticket_id": ticket_id,
        },
    )


def _emit_attempt_cap_attention_event(
    task: TicketTask, client_name: str, lane: str
) -> None:
    """Emit SESSION_NEEDS_ATTENTION when a task is parked at the global attempt ceiling.

    Sibling of :func:`_emit_attempt_cap_blocked_event` (#1257) -- that helper
    emits a DISPATCH_TICK event (operator-visible tick summary); this one
    emits the SESSION_NEEDS_ATTENTION event the attention-monitor/board
    surfaces consume, using the same canonical 9-field payload shape as
    ``_route_scope_gated_approval`` in routing.py. No session/breadcrumb
    detail exists at this pre-spawn ceiling check (the task never spawned a
    session this attempt), so ``session_id``/``session_name``/``breadcrumbs``
    are empty/None.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": client_name,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": DispatchSkipReason.ATTEMPT_CAP_BLOCKED,
            "breadcrumbs": "",
            "crashed": False,
            "lane": lane,
        },
        correlation_id=task.ticket_id,
    )


def _claim_next_pending(
    client_name: str,
    *,
    lane: str,
    config: OrchestratorConfig,
    priority_ticket_ids: list[str] | None = None,
    usage_limited_until: datetime | None = None,
) -> tuple[TicketTask | None, bool]:
    """Atomically claim the next PENDING task for a client in a specific lane.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* in *lane* as RUNNING, saves, and returns it.
    Returns (None, spawn_backoff_skipped) if no pending task exists or all
    eligible tasks are in spawn_error backoff.

    If *priority_ticket_ids* is provided, prefer claiming PENDING tasks in
    that order (only those whose ticket_id appears in the list).  Tasks not
    referenced by the list are skipped at this stage; they will be claimed
    by subsequent ticks once the prioritised tasks are exhausted (the
    parameter is intentionally a *preference*, not a filter — see the
    fallback after the priority loop).

    Global attempt ceiling: if task.attempts >= config.global_attempt_ceiling,
    the task is parked BLOCKED_ON_USER instead of claimed. A dispatch.tick
    event with skip_reason=ATTEMPT_CAP_BLOCKED is emitted per parked task for
    observability. See GitHub #786.

    *usage_limited_until*: when set and still in the future, returns
    ``(None, False)`` immediately without claiming anything (#1346
    defense-in-depth — see the gate below for why this is a parameter, not a
    fresh read).

    Returns a tuple (task, spawn_backoff_skipped) where spawn_backoff_skipped
    is True when at least one PENDING task was skipped due to active
    spawn_error backoff (next_eligible_at in the future). See GitHub #868.
    """
    now = datetime.now(UTC)
    # Defense-in-depth (#1346): the caller (dispatch_tick, via
    # _dispatch_client_lanes) already gates the whole tick on this same
    # value at tick.py's top-of-tick early return -- this second check
    # protects any OTHER caller of this claim primitive (this function is
    # re-exported as part of cw.dispatch's private test surface and is not
    # guaranteed to always be reached only through dispatch_tick's gate).
    # Deliberately takes the value as a parameter rather than calling
    # load_usage_limited_until() here: this function is invoked per-client
    # per-tick (an in-function read would be N reads/tick instead of one),
    # and claim.py is pure state-transition logic under dev_queue_lock with
    # no I/O -- reading the file here would break that invariant.
    if usage_limited_until is not None and now < usage_limited_until:
        return None, False
    with dev_queue_lock():
        store = load_dev_queue()
        spawn_backoff_skipped = False
        if priority_ticket_ids:
            for ticket_id in priority_ticket_ids:
                for task in store.tasks:
                    if (
                        task.client == client_name
                        and task.ticket_id == ticket_id
                        and task.lane == lane
                        and task.status == QueueItemStatus.PENDING
                    ):
                        in_backoff = (
                            task.next_eligible_at is not None
                            and now < task.next_eligible_at
                        )
                        if in_backoff:
                            spawn_backoff_skipped = True
                            break
                        if task.attempts >= config.global_attempt_ceiling:
                            transition_task_status(
                                task,
                                QueueItemStatus.BLOCKED_ON_USER,
                                disposition="attempt_cap_blocked",
                            )
                            save_dev_queue(store)
                            _emit_attempt_cap_blocked_event(client_name, task.ticket_id)
                            _emit_attempt_cap_attention_event(task, client_name, lane)
                            break
                        transition_task_status(task, QueueItemStatus.RUNNING)
                        task.attempts += 1
                        save_dev_queue(store)
                        return task, spawn_backoff_skipped
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
        for task in pending:
            if task.next_eligible_at is not None and now < task.next_eligible_at:
                spawn_backoff_skipped = True
                continue
            if task.attempts >= config.global_attempt_ceiling:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition="attempt_cap_blocked",
                )
                save_dev_queue(store)
                _emit_attempt_cap_blocked_event(client_name, task.ticket_id)
                _emit_attempt_cap_attention_event(task, client_name, lane)
                continue
            transition_task_status(task, QueueItemStatus.RUNNING)
            task.attempts += 1
            save_dev_queue(store)
            return task, spawn_backoff_skipped
    return None, spawn_backoff_skipped


def _lane_occupants_for_client(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, list[dict[str, str]]]:
    """Per-lane occupant ``{ticket_id, status}`` list for dispatch.tick payloads.

    Sibling of :func:`_lane_stats_for_client` -- same OCCUPIED_LANE_STATUSES
    join over ``client``/``lane``, but returns identifying detail instead
    of counts, so a ``lane_cap_blocked`` reader can name the occupant
    instead of inferring a (possibly phantom) cross-client cap. See #1243.

    Deliberately a NEW top-level dispatch.tick payload key, never nested
    inside ``lanes`` -- orchestrate.py's ``_extract_lanes`` hard-filters
    ``lanes`` values to numerics, so a nested ticket-id string would be
    silently stripped downstream.
    """
    occupants: dict[str, list[dict[str, str]]] = {}
    for lane_cfg in client.effective_lanes:
        occupants[lane_cfg.name] = [
            {"ticket_id": t.ticket_id, "status": t.status.value}
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status in OCCUPIED_LANE_STATUSES
        ]
    return occupants


def _lane_stats_for_client(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    occupants: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, dict[str, int]]:
    """Per-lane ``{claimed, running, blocked, signoff, pending}`` counts for
    event payloads.

    Why task-based running: RUNNING/BLOCKED_ON_USER tasks carry ``lane``;
    sessions carry ``lane`` as of #594, but occupancy counting stays task-join
    based per ADR-0006 / Phase 4a scope (stamped-but-not-read by the
    scheduler). BLOCKED_ON_USER occupies its lane slot per ADR-0006, so
    ``running + blocked + signoff`` is the total occupied count. ``blocked``
    and ``signoff`` are split out so operators can see at a glance why
    claimed=0 when pending>0 (#588, #990). Derives running/blocked/signoff
    from :func:`_lane_occupants_for_client` -- see #1243.

    *occupants* lets a caller that already computed the occupant lookup (e.g.
    to also emit ``lane_occupants``/``occupied`` on the same dispatch.tick
    payload) pass it in and avoid a second full scan of ``queue_snapshot.tasks``.
    """
    if occupants is None:
        occupants = _lane_occupants_for_client(client, queue_snapshot)
    stats: dict[str, dict[str, int]] = {}
    for lane_cfg in client.effective_lanes:
        lane_occupants = occupants.get(lane_cfg.name, [])
        running = sum(
            1 for o in lane_occupants if o["status"] == QueueItemStatus.RUNNING.value
        )
        blocked = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.BLOCKED_ON_USER.value
        )
        signoff = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF.value
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
            "signoff": signoff,
            "pending": pending,
        }
    return stats


@dataclass(frozen=True)
class _SpawnOutcome:
    """Result of attempting to spawn one claimed task.

    ``spawned`` — True if a session was started (counters should be bumped).
    ``usage_limit_detected`` — True if a :class:`UsageLimitError` fired.
    ``spawn_error`` — True if a broad spawn failure reverted the task.
    ``error`` — the exception string from a broad spawn failure, or the codex
    capability diagnosis string when ``capability_parked`` is True (``""``
    when neither), carried so the caller can stamp ``last_error`` on the
    per-lane circuit-breaker LANE_PAUSED payload (#875).
    ``capability_parked`` — True if the codex capability gate (#1238) parked
    the task BLOCKED_ON_USER before any spawn was attempted. Below
    :data:`_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD` consecutive parks this is
    deliberately decoupled from ``spawn_error`` (no circuit-breaker increment,
    no aborting the rest of the tick's lane/client loop) since an isolated
    park is a deterministic, per-task condition, not the sporadic failure the
    circuit breaker exists to catch. At/above the threshold, ``spawn_error``
    IS also set (see :func:`_codex_capability_gate`) as a bounded backstop
    against a systemically wrong probe verdict — ``capability_parked`` and
    ``spawn_error`` can both be True at once in that case.

    ``usage_limit_detected`` and ``spawn_error`` signal the caller to break
    out of the slot/lane loops; ``capability_parked`` alone does not (but see
    above — it can co-occur with ``spawn_error`` once the park-count
    threshold is reached).
    """

    spawned: bool = False
    usage_limit_detected: bool = False
    spawn_error: bool = False
    error: str = ""
    capability_parked: bool = False


def _revert_claimed_task_to_pending(
    client_name: str, ticket_id: str, *, stamp_backoff: bool = False
) -> None:
    """Revert a still-RUNNING claimed task back to PENDING, clearing session_id.

    Used by both the usage-limit and broad spawn-error paths: the task was
    claimed to RUNNING by :func:`_claim_next_pending` but spawn never
    succeeded, so it must return to PENDING for a later tick to retry.

    When *stamp_backoff* is True (generic spawn_error path only), increments
    spawn_error_count and sets next_eligible_at to enforce exponential backoff
    before the task is re-claimed.  The usage-limit path passes stamp_backoff=False
    because it has its own fleet-wide backoff mechanism.  See GitHub #868.

    # Why: task.attempts is NOT decremented here. The increment-at-claim
    # contract is intentional — usage_limit deaths and spawn errors consume
    # real dispatch budget and must count toward the global_attempt_ceiling
    # (#786). Decrementing would let churn bypass the backstop. The corollary
    # (#756 stalled-stage cap) uses the same task.attempts field; both caps
    # are outer backstops sharing one counter, not parallel counters.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        for stored_task in store.tasks:
            if (
                stored_task.ticket_id == ticket_id
                and stored_task.client == client_name
                and stored_task.status == QueueItemStatus.RUNNING
            ):
                transition_task_status(stored_task, QueueItemStatus.PENDING)
                stored_task.session_id = None
                if stamp_backoff:
                    stored_task.spawn_error_count += 1
                    delay = min(
                        _SPAWN_ERROR_BACKOFF_INITIAL_SECONDS
                        * (2 ** (stored_task.spawn_error_count - 1)),
                        _SPAWN_ERROR_BACKOFF_CAP_SECONDS,
                    )
                    stored_task.next_eligible_at = datetime.now(UTC) + timedelta(
                        seconds=delay
                    )
                break
        save_dev_queue(store)


def _park_running_task_blocked_on_user(
    *, ticket_id: str, client_name: str, disposition: str, breadcrumbs: str
) -> None:
    """Move a still-RUNNING claimed task to BLOCKED_ON_USER, clearing session_id.

    Shared by every pre-spawn park path that needs to leave a task for operator
    inspection rather than silently retrying it (the dirty-worktree guard and
    the codex capability gate, #1238) — both need the identical lock, load,
    match-by-(ticket_id, client, status==RUNNING), transition, clear
    session_id, save shape; this is the single copy. ``ticket_id``/``client_name``
    are keyword-only (both plain ``str``, no type-system distinction between
    them) so a future edit can't silently transpose them at a call site.

    Also emits SESSION_NEEDS_ATTENTION (#1257) using the canonical 9-field
    payload shape (see ``_route_scope_gated_approval`` in routing.py), reading
    ``stored_task.session_id``/``stored_task.lane`` before ``session_id`` is
    cleared below. ``breadcrumbs`` is caller-supplied human-readable detail
    (e.g. the codex-capability probe's ``.detail`` string, or a stale
    worktree's path) -- distinct from ``disposition``, which is the short
    reason code also stamped as the task's ``disposition``.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        for stored_task in store.tasks:
            if (
                stored_task.ticket_id == ticket_id
                and stored_task.client == client_name
                and stored_task.status == QueueItemStatus.RUNNING
            ):
                transition_task_status(
                    stored_task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=disposition,
                )
                record_event(
                    OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                    {
                        "session_id": stored_task.session_id or "",
                        "session_name": "",
                        "client": client_name,
                        "ticket_id": ticket_id,
                        "claude_session_id": None,
                        "paused_status": disposition,
                        "breadcrumbs": breadcrumbs,
                        "crashed": False,
                        "lane": stored_task.lane,
                    },
                    correlation_id=ticket_id,
                )
                stored_task.session_id = None
                break
        save_dev_queue(store)


def _cached_codex_capability_diagnosis() -> CodexCapabilityDiagnosis:
    """TTL-cached wrapper over :func:`codex_capability_diagnosis` (#1238).

    Mirrors ``gating._resolve_availability``'s cache-and-reuse shape at a
    smaller scope: within ``_CODEX_CAPABILITY_PROBE_TTL_SECONDS`` of the last
    probe, reuse the cached verdict instead of re-shelling ``codex --version``
    on every codex-backed spawn attempt. Process-lifetime only (no sidecar
    persistence) -- unlike the fleet-wide gh-availability latch, this gate has
    no cross-process coordination requirement.
    """
    now = datetime.now(UTC)
    if _codex_capability_cache:
        probe, checked_at = _codex_capability_cache[0]
        if (now - checked_at).total_seconds() < _CODEX_CAPABILITY_PROBE_TTL_SECONDS:
            return probe
    probe = codex_capability_diagnosis(
        timeout_seconds=_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS
    )
    _codex_capability_cache[:] = [(probe, now)]
    return probe


def _reset_codex_capability_cache() -> None:
    """Clear the in-process codex-capability cache/park counter. Test support only.

    (#1238)
    """
    _codex_capability_cache.clear()
    _codex_capability_park_count[0] = 0


def _codex_capability_gate(
    task: TicketTask, client: ClientConfig
) -> _SpawnOutcome | None:
    """Pre-spawn codex capability gate (#1238).

    Returns a parked ``_SpawnOutcome`` (``capability_parked=True``, the
    RUNNING task moved to BLOCKED_ON_USER, session_id cleared, ``disposition``
    set to the probe diagnosis) when the task's stage is codex-backed and the
    ``codex`` CLI is not usable; returns ``None`` to proceed (non-codex
    backend, or codex capable). Reads only cheap, TTL-cached facts — binary
    presence + ``codex --version`` — via the shared
    :func:`_cached_codex_capability_diagnosis` probe; never a live review.

    Below :data:`_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD` consecutive parks,
    does NOT set ``spawn_error`` on the returned outcome: a codex-incapable
    host is a deterministic condition that recurs every tick, not the
    sporadic transient failure the generic spawn-error path (and its per-lane
    circuit breaker) is designed for. Signaling it as a generic spawn_error
    on every occurrence would trip the lane's circuit breaker on the very
    first park (durably pausing the lane, requiring a manual ``cw lane
    resume``) and would abort the rest of that tick's lane/client loop for
    unrelated, non-codex-backed tasks sharing the same lane or client.

    At/above the threshold, the outcome ALSO sets ``spawn_error=True`` — a
    bounded backstop against a systemically wrong probe verdict (the TTL
    cache's result is shared across every codex-backed task in the process,
    so a single bad verdict could otherwise park an unbounded number of tasks
    with no self-limiting mechanism at all).
    """
    if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
        return None
    probe = _cached_codex_capability_diagnosis()
    if probe.diagnosis is None:
        # Capable again -- clear the streak so a fully-recovered condition
        # doesn't leave stale park credit sitting on the counter (#1238
        # review: without this, the counter is a lifetime total, not a
        # consecutive-parks count, and a long-lived dispatch-loop process
        # would eventually treat every future isolated park as
        # breaker-worthy once 3 total parks had *ever* occurred).
        _codex_capability_park_count[0] = 0
        return None
    _log.warning(
        "dispatch: codex capability gate parked %s/%s — %s",
        client.name,
        task.ticket_id,
        probe.detail,
    )
    _park_running_task_blocked_on_user(
        ticket_id=task.ticket_id,
        client_name=client.name,
        disposition=probe.diagnosis,
        breadcrumbs=probe.detail,
    )
    _codex_capability_park_count[0] += 1
    breaker_engaged = (
        _codex_capability_park_count[0] >= _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD
    )
    return _SpawnOutcome(
        capability_parked=True,
        spawn_error=breaker_engaged,
        error=probe.diagnosis,
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
        # Codex capability gate (#1238): a codex-backed stage that cannot reach
        # a usable `codex` CLI parks BLOCKED_ON_USER before any real per-task
        # work runs (worktree creation included) — extracted to a helper to
        # keep this function within the PLR branch/statement budget; no-op for
        # non-codex backends. Runs first so a codex-incapable host never pays
        # for worktree provisioning on a task that's about to be parked.
        parked = _codex_capability_gate(task, client)
        if parked is not None:
            return parked

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
                _park_running_task_blocked_on_user(
                    ticket_id=task.ticket_id,
                    client_name=client.name,
                    disposition="dirty_worktree",
                    breadcrumbs=str(worktree_path_for(client, branch)),
                )
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

        # Function-level import breaks the gating<->claim import cycle:
        # cw.dispatch.gating imports this module at top level, so claim.py
        # must defer its own reach back into gating (mirrors the #698
        # reconcile/_shared -> cw.dispatch precedent the ticket cites).
        from cw.dispatch.gating import _invalidate_stale_context_json

        _invalidate_stale_context_json(task, client, worktree_path)

        executor = resolve_executor(task, client, native_daemon=resolved_native_daemon)
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
                    stored_task.spawn_error_count = 0
                    stored_task.next_eligible_at = None
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
    except Exception as exc:  # noqa: BLE001
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
        _revert_claimed_task_to_pending(client.name, task.ticket_id, stamp_backoff=True)
        return _SpawnOutcome(spawn_error=True, error=str(exc))

    return _SpawnOutcome(spawned=True)
