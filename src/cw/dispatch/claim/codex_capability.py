"""TTL-cached codex capability probe (#1238).

A process-lifetime cache over ``codex --version``. The pre-spawn park that
reads it lives in :func:`cw.dispatch.claim.preflight_reach.preflight_before_spawn`
and the park itself, with its circuit-breaker streak and the
:class:`~cw.dispatch.claim.outcome._SpawnOutcome` record, in
:mod:`cw.dispatch.claim.outcome` (RFC 0019 H2b, #3744).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.dispatch.claim.outcome import reset_park_streak
from cw.executor import (
    CODEX_VERSION_PROBE_TIMEOUT,
    CodexCapabilityDiagnosis,
    codex_capability_diagnosis,
)

# TTL (seconds) for the in-process codex-capability probe cache (#1238). Codex
# CLI presence/version essentially never changes between dispatch ticks, so a
# short process-lifetime cache avoids re-shelling `codex --version` on every
# codex-backed spawn attempt. Unlike gating/availability.py's
# _AVAILABILITY_PROBE_TTL_SECONDS this has no fleet-wide sidecar persistence
# or latch semantics -- it's a per-task gate, not a fleet-wide outage signal,
# so a plain in-memory cache is sufficient.
_CODEX_CAPABILITY_PROBE_TTL_SECONDS = 60

# Shorter TTL (seconds) for a ``CODEX_VERSION_PROBE_TIMEOUT`` verdict (#2908). A
# timeout is transient slowness, not a stable fact about the install, so it must
# not block dispatch for a full 60s. 10s is below the 30s tick interval, so the
# next tick re-probes and recovers promptly, yet long enough to de-duplicate the
# probe across the several codex-backed tasks claimed within one tick. Measured
# from probe completion (see _cached_codex_capability_diagnosis), so the slow
# probe itself does not consume the window.
_CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS = 10

# Timeout for the codex-capability probe's own `codex --version` subprocess
# call when invoked from this hot path (#1238). `_spawn_claimed_task` runs
# synchronously inside dispatch_tick's per-client, per-lane loop, so a stuck
# `codex` binary would otherwise stall that entire tick for up to
# executor.py's one-shot-appropriate 10s default; use a smaller budget here.
# 8s is ~3.5x the ~2.3s warm shell-wrapper latency observed in the field (a 3s
# budget crossed under load and mis-parked healthy tickets, #2908), yet below
# the 10s one-shot default because the probe runs synchronously in the tick.
# Trade-off: with a genuinely hung codex this stalls the tick up to 8s on EVERY
# tick, because the timeout-verdict TTL (10s) is shorter than the 30s
# ``tick_interval_seconds``; the old 3s budget stalled once per 60s window.
_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS = 8

# In-process cache for the codex-capability probe (#1238). A single-element
# list is used as a mutable slot -- updates mutate its contents in place
# rather than rebinding the module-level name, so no `global` statement (and
# no PLW0603 suppression) is needed. Populated lazily by
# _cached_codex_capability_diagnosis; reset via _reset_codex_capability_cache
# (test support only -- production code never needs to invalidate early since
# codex CLI presence/version doesn't change mid-process).
_codex_capability_cache: list[tuple[CodexCapabilityDiagnosis, datetime]] = []


def _cached_codex_capability_diagnosis() -> CodexCapabilityDiagnosis:
    """TTL-cached wrapper over :func:`codex_capability_diagnosis` (#1238).

    Mirrors ``gating.availability._resolve_availability``'s cache-and-reuse shape at a
    smaller scope: within ``_CODEX_CAPABILITY_PROBE_TTL_SECONDS`` of the last
    probe, reuse the cached verdict instead of re-shelling ``codex --version``
    on every codex-backed spawn attempt. Process-lifetime only (no sidecar
    persistence) -- unlike the fleet-wide gh-availability latch, this gate has
    no cross-process coordination requirement.

    A ``CODEX_VERSION_PROBE_TIMEOUT`` verdict uses the shorter
    ``_CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS`` (#2908). The TTL runs from
    probe *completion*: the entry is stamped after the probe returns, so a probe
    that itself took ~8s does not eat its own dedupe window.
    """
    now = datetime.now(UTC)
    if _codex_capability_cache:
        probe, checked_at = _codex_capability_cache[0]
        ttl = (
            _CODEX_CAPABILITY_PROBE_TIMEOUT_TTL_SECONDS
            if probe.diagnosis == CODEX_VERSION_PROBE_TIMEOUT
            else _CODEX_CAPABILITY_PROBE_TTL_SECONDS
        )
        if (now - checked_at).total_seconds() < ttl:
            return probe
    probe = codex_capability_diagnosis(
        timeout_seconds=_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS
    )
    _codex_capability_cache[:] = [(probe, datetime.now(UTC))]
    return probe


def _reset_codex_capability_cache() -> None:
    """Clear the in-process codex-capability cache and park streak. Test support only.

    (#1238; the streak moved to ``cw.dispatch.claim.outcome`` in #3744.)
    """
    _codex_capability_cache.clear()
    reset_park_streak()
