"""Runtime concurrency-override models persisted outside orchestrator.yaml.

``LaneConcurrencyOverride``, ``ClientConcurrencyOverride`` and
``ConcurrencyOverrides``. A leaf of ``cw.models.orchestrator_config``: depends
on pydantic and the stdlib only.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LaneConcurrencyOverride(BaseModel):
    """Per-lane overrides from the concurrency override store."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    max_parallel: int | None = None
    paused: bool | None = None
    # Consecutive spawn_error count for the per-lane circuit breaker (#875).
    # Incremented once per tick on a spawn error, reset to 0 on any success.
    consecutive_spawn_errors: int = 0
    # Debounce stamp for the recurring lane-starved session.needs_attention
    # signal (#1630). Same persisted-timestamp-gate shape as
    # TicketTask.false_park_recovery_next_eligible_at (see
    # cw.reconcile.concierge) -- checked as a plain ``now < next_eligible_at``
    # gate under concurrency_override_lock() and re-armed on every fire -- but
    # a FIXED interval (OrchestratorConfig.lane_starved_notify_interval_minutes),
    # not concierge's exponential backoff: that backoff exists to damp a
    # flapping recovery retry storm, which doesn't apply here -- the operator
    # wants "page me again in N minutes while this lane is still starved", not
    # a growing delay. Cleared by ``cw lane resume`` so a fresh circuit trip
    # after a resume notifies immediately rather than inheriting a stale
    # debounce window from the prior episode.
    lane_starved_notify_next_eligible_at: datetime | None = None


class ClientConcurrencyOverride(BaseModel):
    """Per-client ceiling override from the concurrency override store."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    ceiling: int | None = None
    # Consecutive freshness-gate-block count for the per-client attention latch
    # (RFC 0007 §W2). Incremented once per tick the client is skipped with
    # skip_reason=FRESHNESS_GATE, reset to 0 on the next non-stale tick.
    consecutive_freshness_blocks: int = 0
    # Debounce stamp for the recurring dispatch-loop-staleness
    # session.needs_attention signal (#1875). Deliberately the same shape as
    # LaneConcurrencyOverride.lane_starved_notify_next_eligible_at above --
    # checked as a plain ``now < next_eligible_at`` gate under
    # concurrency_override_lock() and re-armed on every fire, on a FIXED
    # interval (OrchestratorConfig.dispatch_stale_notify_interval_minutes),
    # not an exponential backoff: the operator wants "page me again in N
    # minutes while this client's loop is still not ticking", not a growing
    # delay. Cleared back to None on the first pass that observes the client
    # recovered (a fresh tick, or its pending queue drained), so a later
    # staleness episode notifies immediately rather than inheriting this
    # episode's debounce window.
    dispatch_stale_notify_next_eligible_at: datetime | None = None


class ConcurrencyOverrides(BaseModel):
    """Runtime concurrency overrides persisted outside orchestrator.yaml.

    Written by ``cw config concurrency set`` and ``cw lane pause/resume``.
    Merged with the declared config by ``load_effective_config()``.
    NOT added to schema.REGISTRY — test_schema.py must stay unchanged.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    max_parallel_clients: int | None = None
    clients: dict[str, ClientConcurrencyOverride] = Field(default_factory=dict)
    lanes: dict[str, LaneConcurrencyOverride] = Field(default_factory=dict)
