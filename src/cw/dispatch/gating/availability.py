"""Fleet-wide gh-availability preflight gate for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the TTL-cached
``gh auth status`` probe (RFC 0011 A5), its edge-triggered fleet-wide outage
latch, and the ``dispatch.tick`` skip event for an availability-gated client.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.dispatch_state import (
    AvailabilityProbeCache,
    load_availability_probe_cache,
    save_availability_probe_cache,
)
from cw.events import record_event
from cw.gh import check_gh_availability
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        DevQueueStore,
    )
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


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
