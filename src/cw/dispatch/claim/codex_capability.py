"""Pre-spawn codex capability gate (#1238).

A TTL-cached ``codex --version`` probe, plus the gate that parks a codex-backed
task BLOCKED_ON_USER when the ``codex`` CLI is unusable and engages the lane
circuit breaker once consecutive parks look systemic. Extracted verbatim from
the historical flat ``cw.dispatch.claim`` module by the package split (#2378).
Also holds :class:`_SpawnOutcome`, the spawn-attempt outcome record returned
by both this gate and the spawn path (:mod:`cw.dispatch.claim.spawn`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dispatch.claim.claimed_row import _park_running_task_blocked_on_user
from cw.executor import (
    CodexCapabilityDiagnosis,
    codex_capability_diagnosis,
    resolve_executor_config,
)
from cw.models import CODEX_BACKEND

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger("cw.dispatch")


@dataclass(frozen=True)
class _SpawnOutcome:
    """Result of attempting to spawn one claimed task.

    ``spawned`` — True if a session was started (counters should be bumped).
    ``usage_limit_detected`` — True if a :class:`UsageLimitError` fired.
    ``spawn_error`` — True if a broad spawn failure reverted the task.
    ``occupied`` — True if the reused worktree was occupied by a live cw session
    or daemon worker (``WorktreeOccupiedError``, #2213) and the claim was
    released without a spawn -- or, since #2077, the hook-context write found
    it held by a genuinely live session (a ``HookContextConflictError`` with
    ``genuinely_live`` set, see :func:`_defer_genuinely_live_hook_conflict`),
    which reuses this same field. Deliberately decoupled from ``spawn_error``: an
    occupied worktree is a transient, per-ticket condition, not the sporadic
    backend failure the circuit breaker exists to catch, so it neither
    increments the lane's spawn-error count nor aborts the rest of the tick's
    lane/client loop.
    ``error`` — the exception string from a broad spawn failure, the codex
    capability diagnosis string when ``capability_parked`` is True, or the
    occupancy reason when ``occupied`` is True (``""`` when none of these),
    carried so the caller can stamp ``last_error`` on the per-lane
    circuit-breaker LANE_PAUSED payload (#875).
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
    ``usage_limit_reset_at`` — the instant the limit lifts, when the spawn-time
    message named one that parsed (#1409); None when it did not, which the
    dispatch loop reads as "use the flat ``usage_limit_backoff_seconds``". Only
    ever set alongside ``usage_limit_detected``. Declared last so the existing
    positional-free call sites are unaffected.
    """

    spawned: bool = False
    usage_limit_detected: bool = False
    spawn_error: bool = False
    error: str = ""
    capability_parked: bool = False
    occupied: bool = False
    usage_limit_reset_at: datetime | None = None


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
        unproductive=False,
        created_at=task.created_at,
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
