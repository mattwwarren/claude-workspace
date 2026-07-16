"""Pydantic models for session state and client configuration."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"
    ORCHESTRATE = "orchestrate"


# Purposes a worker session can be dispatched/created with. ORCHESTRATE is
# excluded: an ORCHESTRATE session is created only via `cw orchestrate start`
# (#595 / Phase 4b), never selected as a worker --purpose.
WORKER_PURPOSES: tuple[SessionPurpose, ...] = (
    SessionPurpose.IMPL,
    SessionPurpose.IDEA,
    SessionPurpose.DEBT,
)


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"
    # Headless daemon session exceeded wall-clock budget without emitting a
    # sentinel. Terminal-ish but retry-eligible: reconciler reverts the
    # owning TicketTask to PENDING so the dispatch loop can retry.
    # See GitHub issue #176 Layer 1.
    TIMED_OUT = "timed_out"


class CompletionReason(StrEnum):
    USER = "user"
    HANDOFF = "handoff"
    CRASHED = "crashed"
    NORMAL = "normal"
    TIMED_OUT = "timed_out"


class SessionOrigin(StrEnum):
    USER = "user"
    DAEMON = "daemon"


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_ON_USER = "blocked_on_user"
    # RFC 0007 Phase 3 (W3): a ticket parked for an explicit operator signoff
    # before it ships (occupies its lane slot the same as BLOCKED_ON_USER).
    # See GitHub #990.
    AWAITING_OPERATOR_SIGNOFF = "awaiting_operator_signoff"


# Statuses that occupy a lane's concurrency slot (RUNNING/BLOCKED_ON_USER
# already did per ADR-0006; AWAITING_OPERATOR_SIGNOFF joins them in #990 --
# a signoff-parked ticket is not eligible for re-dispatch, so it must not be
# double-counted as free capacity). Single source of truth for the 4+
# occupancy-membership tests duplicated across dispatch.py/board.py/
# config_cmds.py prior to this ticket.
OCCUPIED_LANE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)


class ReapReason(StrEnum):
    """Reason taxonomy for queue.session_reaped bus events.

    Each value maps to exactly one reconcile disposition path. See the
    reap-site decision table in GitHub issue #380.
    """

    PHANTOM_SURFACE = "phantom_surface"
    IDLE_STALL = "idle_stall"
    USAGE_LIMIT_CUTOFF = "usage_limit_cutoff"
    RETRY_CAP_PARKED = "retry_cap_parked"
    STALLED_RETRY_CAP_PARKED = "stalled_retry_cap_parked"
    WALL_CLOCK_BUDGET = "wall_clock_budget"
    COMPLETED_BACKSTOP = "completed_backstop"
    SALVAGE_COMPLETED = "salvage_completed"
    SALVAGE_PARKED = "salvage_parked"
    FINALIZE_BLOCKED = "finalize_blocked"
    TERMINAL_SIBLING = "terminal_sibling"


class Stage(StrEnum):
    """RFC 0005 pipeline stage. Mutated live by dispatch via
    ``_stage_advance_unchecked`` as a task moves through the pipeline."""

    HARDEN = "harden"
    PLAN = "plan"
    IMPL = "impl"
    REVIEW = "review"
    FINALIZE = "finalize"


class LivenessBucket(StrEnum):
    """RFC 0008 W2 transcript-staleness bucket, latched onto ``Session``.

    Closed 4-value set: per-stage overrides
    (``OrchestratorConfig.liveness_first_bucket_by_stage``) move the
    entry-point threshold for a stage, but never rename or add labels.
    Downstream consumers of ``session.liveness_changed`` match these
    literals directly without reading config. See GitHub #1001.
    """

    LIVE = "live"
    STALE_15M = "stale_15m"
    STALE_30M = "stale_30m"
    STALE_45M = "stale_45m"


# Schema versions for persisted state. Bump when making a breaking change
# to the on-disk layout; add a migration in `cw.config.migrate_cw_state`
# or `cw.dev_queue.migrate_dev_queue` to handle older versions.
# v6: added Session.idle_observation_count (GitHub #545).
# v7: added Session.reap_reason (GitHub #380).
# v8: added Session.reap_proposed_at (GitHub #555).
# v9: added Session.lane (GitHub #594).
# v10: added Session.stage (GitHub #612).
# v11: added Session.local_liveness (GitHub #888).
# v12: added Session.consecutive_salvage_skips (#974).
# v13: added Session.liveness_bucket (GitHub #1001, RFC 0008 W2).
# v14: local_liveness.start_time_ns reference point changed from
#      boot-relative (/proc) to epoch-relative (psutil.create_time); stale
#      pre-v14 handles are cleared on migration so they don't false-positive
#      as "dead" against a live process re-read in the new format (GitHub #921).
CW_STATE_SCHEMA_VERSION = 14
# v3: added TicketTask.lane (GitHub #557).
# v4: added TicketTask.stage + stage_base_ref (GitHub #612).
# v5: added TicketTask.disposition, pr_url, completed_at (GitHub #310).
# v6: added TicketTask.regress_attempts (GitHub #770).
# v7: added TicketTask.spawn_error_count, next_eligible_at (GitHub #868).
# v8: added TicketTask.pr_state (GitHub #929).
# v9: added TicketTask.signoff (GitHub #990).
# v10: added TicketTask.escalation_parked_at/escalation_fired_at
#      (GitHub #1015, RFC 0008 capstone).
# v11: added TicketTask.false_park_recovery_count/
#      false_park_recovery_next_eligible_at (GitHub #1030).
# v12: added TicketTask.gate_recipe_failed_at (GitHub #1065, RFC 0009).
# v13: added TicketTask.gate_recipes + LaneConfig.gate_recipes (GitHub #1067,
#      RFC 0009 P4).
# v14: added TicketTask.escalate_merge_block_fired_at (GitHub #1099, RFC 0010
#      P4) — one-shot latch for the escalate_merge_block review recipe.
# v15: added DevQueueStore.watched_prs (GitHub #1154, RFC 0011 S2) — a
#      top-level list of externally-requested PRs the operator is watching,
#      distinct from the per-task queue.
# v16: added TicketTask.request_reviewer_fired_at (GitHub #1197) — one-shot
#      latch for the request_reviewer review recipe.
DEV_QUEUE_SCHEMA_VERSION = 16
DEFAULT_LANE: str = "default"
DEFAULT_STAGE: Stage = Stage.PLAN


class LaneConcurrencyOverride(BaseModel):
    """Per-lane overrides from the concurrency override store."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    max_parallel: int | None = None
    paused: bool | None = None
    # Consecutive spawn_error count for the per-lane circuit breaker (#875).
    # Incremented once per tick on a spawn error, reset to 0 on any success.
    consecutive_spawn_errors: int = 0


class ClientConcurrencyOverride(BaseModel):
    """Per-client ceiling override from the concurrency override store."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    ceiling: int | None = None
    # Consecutive freshness-gate-block count for the per-client attention latch
    # (RFC 0007 §W2). Incremented once per tick the client is skipped with
    # skip_reason=FRESHNESS_GATE, reset to 0 on the next non-stale tick.
    consecutive_freshness_blocks: int = 0


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


class OrchestratorEventType(StrEnum):
    """Event types for the orchestrator-level event bus.

    Covers PR lifecycle, ticket queue, and cross-session coordination.
    """

    TICKET_ENQUEUED = "ticket.enqueued"
    SESSION_SPAWNED = "session.spawned"
    SESSION_COMPLETED = "session.completed"
    SESSION_TIMED_OUT = "session.timed_out"
    SESSION_NEEDS_ATTENTION = "session.needs_attention"
    TICKET_NEEDS_SYNC = "ticket.needs_sync"
    STAGE_ENTERED = "stage.entered"
    STAGE_ERRORED = "stage.errored"
    PR_REGISTERED = "pr.registered"
    PR_CI_FAILED = "pr.ci_failed"
    PR_REVIEW_RECEIVED = "pr.review_received"
    PR_MERGEABLE = "pr.mergeable"
    PR_MERGED = "pr.merged"
    DISPATCH_TICK = "dispatch.tick"
    DISPATCH_LOOP_EXITED = "dispatch.loop_exited"
    SESSION_PHANTOM_REVERTED = "session.phantom_reverted"
    SESSION_SALVAGE_SKIPPED = "session.salvage_skipped"
    SESSION_REAP_PROPOSED = "session.reap_proposed"
    SESSION_REAP_AUTHORIZED = "session.reap_authorized"
    SESSION_SPAWN_UNREGISTERED = "session.spawn_unregistered"
    LANE_CREATED = "lane.created"
    LANE_PAUSED = "lane.paused"
    LANE_RESUMED = "lane.resumed"
    TICKET_MOVED = "ticket.moved"
    TICKET_APPROVED = "ticket.approved"
    TICKET_REQUEUED = "ticket.requeued"
    TICKET_UNBLOCKED = "ticket.unblocked"
    SESSION_STAGE_TIMED_OUT_RETRIED = "session.stage_timed_out_retried"
    WAVE_COLLISION = "wave.collision"
    # RFC 0008 W1 orchestrator push-channel producers (closes #978). Emitted
    # from cw.dev_queue at the status-authority / stage-mutation / row-removal
    # chokepoints; correlation_id is the ticket_id. See docs/events.md.
    TASK_TRANSITION = "task.transition"
    TASK_STAGE_CHANGED = "task.stage_changed"
    TASK_DELETED = "task.deleted"
    # RFC 0008 W2 liveness producer (#1001): latched transcript-staleness
    # bucket crossings from the reconcile idle-watchdog pass.
    SESSION_LIVENESS_CHANGED = "session.liveness_changed"
    # RFC 0008 capstone (#1015) — daemon-side gate concierge. CONCIERGE_RECOVERED
    # is the mechanical-recovery-reactor's audit trail (emitted before every
    # recipe's mutation); OPERATOR_ESCALATION is the durable-escalation-latch's
    # one-shot fire when a gate has sat parked past ESCALATION_PARK_MINUTES.
    CONCIERGE_RECOVERED = "concierge.recovered"
    OPERATOR_ESCALATION = "operator.escalation"
    # GitHub #1019 — sentinel/task stage-mismatch guard. Emitted by
    # _route_staged_decision when a late/replayed sentinel's stage_reached
    # does not match task.stage; the routing table refuses to advance the
    # row in that case (true no-op — see cw.dispatch._STAGE_REACHED_TO_STAGE).
    SENTINEL_STAGE_MISMATCH = "sentinel.stage_mismatch"
    # #976 — wall-clock-budget liveness veto. Emitted by the stalled sweep
    # instead of proceeding with a REVERT_TASK/park when the session's
    # freshly-classified liveness bucket is still LIVE despite the wall-clock
    # budget having expired. Side-effect-only: no queue or session mutation.
    SESSION_PARK_VETOED = "session.park_vetoed"
    # GitHub #1030 — concierge recipe 1 (false_park_requeue) churn backoff.
    # Emitted from _act_on_false_park_candidates when a candidate's session
    # shows the dead-on-arrival signature (died within seconds of spawn,
    # never producing real output) — the requeue to PENDING always proceeds
    # regardless; this event additionally records that
    # false_park_recovery_count / false_park_recovery_next_eligible_at were
    # stamped, deferring the *next* false-park detection cycle for this
    # ticket. NOT a veto (contrast SESSION_PARK_VETOED above, which
    # accompanies zero mutation) — no queue/session mutation is suppressed.
    CONCIERGE_RECOVERY_BACKOFF_ARMED = "concierge.recovery_backoff_armed"
    # RFC 0009 P1+P2 (#1065) — gate-recipe automation. Emitted by
    # cw.reconcile.gate_recipes._act_auto_approve_review before the
    # auto-approve mutation when a review met the fixed clean-review predicate
    # (no MUST_FIX, no deferred, recommendation=PROCEED, no forbidden-area
    # touch) and was approved with no human review. Unlike CONCIERGE_RECOVERED
    # (audit-only), this IS forwarded to the operator channel by default — an
    # auto-approve bypassing human review is attention-worthy.
    GATE_AUTO_APPROVED = "gate.auto_approved"
    # RFC 0009 P1+P2 (#1065) — companion to GATE_AUTO_APPROVED. Emitted when
    # the act-phase mutation raises after GATE_AUTO_APPROVED was already
    # recorded (e.g. a duplicate row, or the client's pipeline config changed
    # between detect and act) — so the durable event stream carries a
    # correction, not just a non-durable log line, for what would otherwise
    # be a false "approved" signal on the operator channel. Forwarded by
    # default alongside GATE_AUTO_APPROVED for the same reason.
    GATE_AUTO_APPROVE_FAILED = "gate.auto_approve_failed"
    # RFC 0010 P2 (#1097) — review-recipe act phase. Emitted by
    # cw.reconcile.review_recipes._act_address_review BEFORE dispatching an
    # /address-review session in response to a PR whose review came back
    # changes_requested. Like GATE_AUTO_APPROVED (contrast CONCIERGE_RECOVERED,
    # audit-only), this IS forwarded to the operator channel by default — an
    # automated PR action with no human in the loop is attention-worthy. Reused
    # by RFC 0010 P4's other review recipes with no new event types.
    PR_ACTION_TAKEN = "pr.action_taken"
    # RFC 0010 P2 (#1097) — companion to PR_ACTION_TAKEN. Emitted when the
    # dispatch (spawn_create_impl) raises after PR_ACTION_TAKEN was already
    # recorded, or when a precondition anomaly (unparseable PR url, unresolvable
    # client, missing worktree) blocks the action — so the durable event stream
    # carries a correction, not just a non-durable log line. Forwarded by
    # default alongside PR_ACTION_TAKEN for the same reason.
    PR_ACTION_FAILED = "pr.action_failed"


# Absolute ceiling on task.attempts across all kill causes (#786).
# Lives here so OrchestratorConfig.global_attempt_ceiling can reference it
# directly without a circular import (dispatch.py imports from models.py).
DEFAULT_GLOBAL_ATTEMPT_CEILING = 10


class DispatchSkipReason(StrEnum):
    """First-match skip_reason values emitted in dispatch.tick events.

    Precedence (highest first):
    FRESHNESS_GATE > USAGE_LIMITED > CAP_FULL > LANE_CAP_BLOCKED
    > SPAWN_ERROR > LANE_CIRCUIT_PAUSED > SPAWN_ERROR_BACKOFF > NO_PENDING
    > NONE.
    ATTEMPT_CAP_BLOCKED is emitted per-task when the global attempt ceiling
    parks a task; it is not part of the per-client-tick precedence chain.
    """

    FRESHNESS_GATE = "freshness_gate"
    USAGE_LIMITED = "usage_limited"
    CAP_FULL = "cap_full"
    LANE_CAP_BLOCKED = "lane_cap_blocked"
    ATTEMPT_CAP_BLOCKED = "attempt_cap_blocked"
    SPAWN_ERROR = "spawn_error"
    LANE_CIRCUIT_PAUSED = "lane_circuit_paused"
    SPAWN_ERROR_BACKOFF = "spawn_error_backoff"
    NO_PENDING = "no_pending"
    NONE = "none"


class OrchestratorEvent(BaseModel):
    """A single event on the orchestrator event bus."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    type: OrchestratorEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consumed_at: datetime | None = None


class PrState(BaseModel):
    """Hydrated GitHub PR state persisted on a TicketTask (GitHub #929).

    Populated by the serve-tick hydration pass (``cw.pr_hydrate``) from a
    ``gh pr view --json`` response. ``attention_state`` is the operator-facing
    escalation signal derived by ``_compute_attention_state``; None for drafts
    and terminal (MERGED/CLOSED) PRs. ``failing_checks`` carries the failing
    check names for the ``pr.ci_failed`` event payload. ``is_draft``,
    ``reviewer_count``, and ``pending_count`` are the remaining
    ``_compute_attention_state`` ladder inputs the poll path always computes
    but previously never persisted (#1196) — storing them lets the webhook
    push path recompute ``attention_state`` from the carried baseline without
    re-fetching GitHub.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    state: str = "OPEN"
    mergeable: str | None = None
    merge_state_status: str = "UNKNOWN"
    ci_ok: bool = True
    review_decision: str = ""
    attention_state: str | None = None
    is_draft: bool = False
    reviewer_count: int = 0
    pending_count: int = 0
    failing_checks: list[str] = Field(default_factory=list)
    hydrated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchedPr(BaseModel):
    """An externally-requested PR the operator is watching (GitHub #1154).

    Registered when someone requests the operator's review on a PR the queue
    does not otherwise track — via ``cw review register <pr>`` (``source="cli"``)
    or the ``review_requested`` webhook (``source="webhook"``). Persisted as a
    top-level ``DevQueueStore.watched_prs`` entry (RFC 0011 S2), deliberately
    NOT a ``TicketTask``: it carries no ``client``/``lane`` and never occupies a
    dispatch lane slot. ``pr_state`` is hydrated by the serve-tick pass
    (``cw.pr_hydrate._hydrate_watched_prs``) exactly like ``TicketTask.pr_state``.

    ``status`` reserves a ``"dismissed"`` terminal that no code sets this slice —
    the ``(repo, pr_number)`` dedup guard is scoped to ``"active"`` so a future
    dismiss transition can re-open registration (RFC 0011 S2, adopted #5).
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    pr_url: str
    repo: str
    pr_number: int
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requester_login: str | None = None
    source: Literal["webhook", "cli"]
    status: Literal["active", "dismissed"] = "active"
    pr_state: PrState | None = None


def _validate_gate_recipe_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized gate-recipe key (RFC 0009 P4).

    Shared by the ``gate_recipes`` field validators on both
    :class:`TicketTask` and :class:`LaneConfig`. Local literal, not an import
    of cw.reconcile.gate_recipes's RECIPE_* constants — models.py sits below
    cw.reconcile in the import graph (reconcile imports from models, not the
    reverse), so importing them here would be circular. Mirrors
    OrchestratorConfig._validate_concierge_recoveries_keys's fail-loud stance:
    a typo'd key would otherwise silently resolve to the hardcoded default-off
    via resolve_gate_recipe_enabled's plain ``in`` check, leaving the intended
    recipe disabled with zero error at config-load time.
    """
    recognized = {"auto_approve_clean_review", "auto_adopt_clean_plan"}
    unknown = sorted(set(value) - recognized)
    if unknown:
        msg = (
            f"gate_recipes has unrecognized recipe key(s): {unknown}. "
            f"Recognised keys: {sorted(recognized)}."
        )
        raise ValueError(msg)
    return value


def _validate_review_recipe_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized review-recipe key (RFC 0010 P3, #1098).

    Shared by the ``review_recipes`` field validators on both
    :class:`TicketTask` and :class:`LaneConfig`. Local literal, not an import
    of cw.reconcile.review_recipes's RECIPE_ADDRESS_REVIEW — models.py sits
    below cw.reconcile in the import graph (reconcile imports from models, not
    the reverse), so importing it here would be circular. Mirrors
    _validate_gate_recipe_keys's fail-loud stance: a typo'd key would otherwise
    silently resolve to the hardcoded default-off via
    resolve_review_recipe_enabled's plain ``in`` check, leaving the intended
    recipe disabled with zero error at config-load time.
    """
    recognized = {
        "address_review",
        "auto_fix_ci",
        "request_reviewer",
        "escalate_merge_block",
    }
    unknown = sorted(set(value) - recognized)
    if unknown:
        msg = (
            f"review_recipes has unrecognized recipe key(s): {unknown}. "
            f"Recognised keys: {sorted(recognized)}."
        )
        raise ValueError(msg)
    return value


# ticket_id feeds into f"{feature_branch_prefix}/{ticket_id}" (dispatch.py),
# raw `gh` argv slots, and gh API URL path segments (gh.py). Sibling to
# _SAFE_CLIENT_NAME / _SAFE_BRANCH_NAME in cw.config (duplicated here, not
# imported, to avoid a models<->config import cycle -- config.py already
# imports from cw.models). No '/': ticket_id is a path *component*, the
# branch prefix supplies the separator. No '..': blocks the gh-api
# path-segment-confusion case in _fetch_branch_exists_on_origin. See #1129.
#
# '#' IS permitted: `repo#N` is a real tracker id shape in production use, and
# #1129's original charset outlawed it, which bricked load_dev_queue() for every
# client the moment one such row hit disk. '#' is legal in a git ref name and
# inert in argv (subprocess takes a list — no shell). Its one real hazard is
# acting as a URL fragment inside a `gh api` path, and that is handled by
# percent-encoding at the sink (cw.gh._fetch_branch_exists_on_origin) rather
# than by outlawing an id the tracker already assigned.
_SAFE_TICKET_ID = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9][a-zA-Z0-9._#-]*$")


class TicketTask(BaseModel):
    """A ticket queued for dispatch to a Claude session."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    ticket_id: str
    client: str
    priority: int = 0
    worktree_path: Path | None = None
    linear_url: str | None = None
    scope_hint: str | None = None
    status: QueueItemStatus = QueueItemStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Stamped by dispatch_tick after spawn_create_impl returns; cleared on
    # reconcile revert. Used by consume_completed_sessions to disambiguate
    # SESSION_COMPLETED events from old (crashed) sessions that share a
    # ticket_id with a freshly-respawned task. None for legacy tasks
    # persisted before this field existed — consumer falls back to
    # ticket_id-only matching in that case.
    session_id: str | None = None
    # Incremented each time the task is claimed by _claim_next_pending. Used to
    # apply a hard cap on validation_failed retries (see issue #251).
    attempts: int = 0
    # Number of times the task has been auto-regressed from FINALIZE back to
    # IMPL for self-heal (e.g. diff-cover gate failures). Bounded by
    # FINALIZE_REGRESS_CAP in auto_dev_result.py. See GitHub #770.
    regress_attempts: int = 0
    # Per-ticket wall-clock budget override (seconds). When set, takes precedence
    # over the per-tier default in OrchestratorConfig.headless_timeout_by_tier and
    # the global HEADLESS_TIMEOUT_SECONDS fallback. Set via ``cw dev-queue add
    # --timeout <s>``. None means "use tier or global default". See issue #265.
    headless_timeout_override: int | None = None
    # Per-ticket idle-watchdog budget override (seconds). When set, takes precedence
    # over the per-tier default in OrchestratorConfig.idle_watchdog_by_tier and
    # the global IDLE_WATCHDOG_SECONDS fallback. None means "use tier or global
    # default". See GitHub issue #326.
    idle_watchdog_override: int | None = None
    # Cumulative USD cost across all auto-dev attempts for this ticket.
    # Populated by _accumulate_task_cost in consume_completed_sessions.
    # None when no cost data has been recorded yet. See GitHub issue #124.
    total_cost_usd: float | None = None
    # The source of the implementation plan for this ticket. Carried into
    # cw-context.json so workers can emit the correct plan_source in their
    # AutoDevResult sentinel without rediscovering it at runtime (#314).
    # Always None today; populated by a future dev-queue plan command.
    plan_source: str | None = None
    # Pipeline-computed scope tier ("small"/"large") stamped onto the task by
    # dispatch's _persist_carried_context after each stage completes (from the
    # sentinel's scope.tier). Distinct from scope_hint (operator hint, escalate-
    # only in _resolve_scope_tier) -- kept separate so a computed tier can never
    # overwrite an operator escalation. Carried into cw-context is intentionally
    # deferred to a follow-up ticket (#1050 introduced this field; wiring it
    # into cw-context.json is out of scope there); today it records
    # provenance on the row.
    computed_scope_tier: str | None = None
    # Lane this ticket is assigned to. Defaults to DEFAULT_LANE; set by
    # orchestrate/dispatch in Phase 2 (#558).
    lane: str = DEFAULT_LANE
    # RFC 0005 A1 — dormant; no dispatch wiring yet (GitHub #612).
    stage: Stage = DEFAULT_STAGE
    stage_base_ref: str | None = None
    # Terminal disposition for this ticket — the AutoDevResult status (or a
    # reconcile reason string) that caused the task to reach COMPLETED,
    # BLOCKED_ON_USER, or FAILED.  Cleared on PENDING/CANCELLED (requeue/cancel).
    # None for in-flight or pre-v5 legacy tasks.  GitHub #310.
    disposition: str | None = None
    # PR URL for shipped tasks (disposition="shipped").  None otherwise.
    pr_url: str | None = None
    # Timestamp when the task reached a terminal status (COMPLETED/BLOCKED_ON_USER/
    # FAILED).  Cleared on requeue/cancel.  None for in-flight or pre-v5 legacy.
    completed_at: datetime | None = None
    # Exponential backoff state for spawn_error retries (GitHub #868).
    # spawn_error_count tracks consecutive failures; next_eligible_at is the
    # earliest timestamp at which _claim_next_pending will re-claim this task.
    # Both are cleared atomically on a successful spawn.
    spawn_error_count: int = 0
    next_eligible_at: datetime | None = None
    # Exponential backoff state for concierge recipe 1's false-park recovery
    # (GitHub #1030) — a distinct field pair from spawn_error_count/
    # next_eligible_at above: that pair covers subprocess spawn errors at the
    # dispatch claim gate; these cover "the previous mechanical recovery
    # produced a session that died instantly" at concierge's own detect
    # phase. Both are cleared (reset to 0/None) when a recovery is NOT
    # dead-on-arrival (a legitimate stall).
    false_park_recovery_count: int = 0
    false_park_recovery_next_eligible_at: datetime | None = None
    # Hydrated GitHub PR state (merge/CI/review) persisted by the serve-tick
    # hydration pass (cw.pr_hydrate). None until first hydration or for pre-v8
    # legacy tasks. See GitHub #929.
    pr_state: PrState | None = None
    # Ticket-level operator-signoff override (RFC 0007 Phase 3). Takes
    # precedence over LaneConfig.signoff and OrchestratorConfig.default_signoff
    # in resolve_signoff's 3-tier resolution. None means "no ticket-level
    # override -- fall through to lane/global". Set via
    # ``cw dev-queue add --signoff operator``. See GitHub #990.
    signoff: Literal["operator"] | None = None
    # Ticket-level gate-recipe enablement override (RFC 0009 P4, #1067). Highest
    # tier in resolve_gate_recipe_enabled's 3-tier precedence: a recipe present
    # here wins over LaneConfig.gate_recipes and the hardcoded default-off. None
    # (or a recipe absent from the map) defers to the lane map, then the
    # default. Recognised keys: "auto_approve_clean_review",
    # "auto_adopt_clean_plan".
    gate_recipes: dict[str, bool] | None = None
    # Ticket-level review-recipe enablement override (RFC 0010 P3, #1098).
    # Highest tier in resolve_review_recipe_enabled's 3-tier precedence: a
    # recipe present here wins over LaneConfig.review_recipes and the hardcoded
    # default-off. None (or a recipe absent from the map) defers to the lane
    # map, then the default. Recognised keys: "address_review", "auto_fix_ci",
    # "request_reviewer", "escalate_merge_block" (RFC 0010 P4, #1099). Sibling
    # to gate_recipes, not a reuse: review recipes react to PR attention states
    # (changes_requested / ci_failing / no_reviewer / merge_blocked), a distinct
    # action class from the approval-gate recipes above.
    review_recipes: dict[str, bool] | None = None
    # RFC 0008 capstone (#1015) — durable escalation latch. Stamped by
    # cw.reconcile.escalation.run_escalation_sweep when this task first enters
    # the escalation-eligible set (see that module's docstring for the
    # 6-gate formula); escalation_fired_at is stamped once, when the parked
    # window exceeds ESCALATION_PARK_MINUTES, gating a single
    # OPERATOR_ESCALATION emission per parked episode. Both are cleared
    # unconditionally by transition_task_status on every status transition
    # (Q5) — the single mutation seam this task's status/disposition ever
    # goes through, so a fresh parked episode always starts with a clean
    # latch regardless of which call site (approve/requeue/cancel/unblock/
    # advance) ended the previous one.
    escalation_parked_at: datetime | None = None
    escalation_fired_at: datetime | None = None
    # RFC 0009 P1+P2 (#1065) — one-shot gate-recipe failure latch. Stamped by
    # cw.reconcile.gate_recipes._act_auto_approve_review when the act-phase
    # mutation raises after GATE_AUTO_APPROVED was already emitted; a
    # non-None value excludes this row from _detect_auto_approve_review so a
    # persisting failure (e.g. stale client config, a duplicate row) does not
    # re-detect and re-emit GATE_AUTO_APPROVED/GATE_AUTO_APPROVE_FAILED every
    # reconcile tick forever. Cleared the same way the escalation latch above
    # is: unconditionally, by transition_task_status on every status
    # transition (including a same-status re-park) — so a fresh review
    # episode (new session, new last_result re-parking the row at
    # BLOCKED_ON_USER) always starts with a clean latch.
    gate_recipe_failed_at: datetime | None = None
    # RFC 0010 P4 (#1099) — one-shot latch for the escalate_merge_block review
    # recipe (cw.reconcile.review_recipes). Stamped by _act_escalate_merge_block
    # when it emits PR_ACTION_TAKEN for a merge_blocked PR, so the escalation
    # fires exactly once per merge-blocked episode rather than every reconcile
    # tick. Distinct from escalation_parked_at/escalation_fired_at above (whose
    # eligibility keys off task.status/disposition — an orthogonal trigger to
    # pr_state.attention_state): sharing the field would race two subsystems
    # under different semantics. Cleared by _act_escalate_merge_block's own
    # episode-end sweep when the row's pr_state leaves merge_blocked (or goes
    # None), re-arming the latch for a genuine future re-entry.
    escalate_merge_block_fired_at: datetime | None = None
    # GitHub #1197 — one-shot latch for the request_reviewer review recipe
    # (cw.reconcile.review_recipes). Stamped by _prepare_request_reviewer_job
    # when it emits PR_ACTION_TAKEN for a no_reviewer PR, so the reviewer
    # request fires exactly once per no-reviewer episode rather than every
    # reconcile tick. Cleared by _act_request_reviewer's own episode-end sweep
    # when the row's pr_state leaves no_reviewer (or goes None), re-arming the
    # latch for a genuine future re-entry — mirrors
    # escalate_merge_block_fired_at above.
    request_reviewer_fired_at: datetime | None = None

    @field_validator("gate_recipes")
    @classmethod
    def _check_gate_recipes(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_gate_recipe_keys(value)

    @field_validator("review_recipes")
    @classmethod
    def _check_review_recipes(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_review_recipe_keys(value)

    @field_validator("ticket_id")
    @classmethod
    def _check_ticket_id(cls, value: str) -> str:
        # "" is a deliberate sentinel for "no associated ticket"
        # (reconcile/local.py's LOCAL DAEMON harvest path); only
        # non-empty values are format-checked.
        if value and not _SAFE_TICKET_ID.match(value):
            msg = (
                f"Invalid ticket_id {value!r}: must start with alphanumeric"
                " and contain only [a-zA-Z0-9._-], with no '..' sequence"
            )
            raise ValueError(msg)
        return value


class DispatchPlan(BaseModel):
    """Ordered list of tickets to dispatch, with optional grouping hints."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    tasks: list[TicketTask] = Field(default_factory=list)
    grouping_hints: dict[str, str] = Field(default_factory=dict)


class DevQueueStore(BaseModel):
    """Persisted dev-queue state holding TicketTasks."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    schema_version: int = DEV_QUEUE_SCHEMA_VERSION
    tasks: list[TicketTask] = Field(default_factory=list)
    watched_prs: list[WatchedPr] = Field(default_factory=list)

    def pending(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.PENDING]

    def running(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.RUNNING]

    def completed(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.COMPLETED]

    def cancelled(self) -> list[TicketTask]:
        return [t for t in self.tasks if t.status == QueueItemStatus.CANCELLED]

    def by_client(self, client: str) -> list[TicketTask]:
        return [t for t in self.tasks if t.client == client]


class ReapPolicy(StrEnum):
    """Policy controlling whether the reconciler destroys a stalled session.

    Under ``SIGNAL_ONLY`` (default): route the owning task to BLOCKED_ON_USER,
    leave session/worktree/daemon surface intact.  Requires operator action to clear.
    Under ``AUTO``: self-healing — stop daemon, revert task to PENDING, clean worktree.
    """

    SIGNAL_ONLY = "signal_only"
    AUTO = "auto"


CLAUDE_NATIVE_BACKEND: str = "claude-native"
LOCAL_BACKEND: str = "local"
CODEX_BACKEND: str = "codex"

# Relative path of the per-worktree materialized ticket context, shared by
# dispatch's pre-spawn invalidation (#1046) and local_runner's prompt builder
# so the two never drift onto different literal paths for the same file.
CONTEXT_JSON_RELATIVE_PATH: Path = Path(".cw", "context.json")


class StageExecutorConfig(BaseModel):
    """Executor configuration for a single pipeline stage (RFC 0005 A1, dormant)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = CLAUDE_NATIVE_BACKEND
    model: str | None = None
    endpoint: str | None = None  # OpenAI-compatible base URL for local backend


class StagePipelineConfig(BaseModel):
    """Per-client (or per-lane) pipeline configuration (RFC 0005 A1, dormant)."""

    model_config = ConfigDict(extra="forbid")

    stages: list[Stage] = Field(
        default_factory=lambda: [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]
    )
    executors: dict[Stage, StageExecutorConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stages_unique(self) -> StagePipelineConfig:
        if len(self.stages) != len(set(self.stages)):
            msg = "pipeline stages must be unique"
            raise ValueError(msg)
        return self


class LaneConfig(BaseModel):
    """Configuration for a named dispatch lane.

    Lanes provide a scheduling boundary for TicketTasks.
    Phase 1 (data model only): no dispatch wiring yet — see #558.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    max_parallel: int = 1
    priority: int = 0
    paused: bool = False
    description: str = ""
    reap_policy: ReapPolicy | None = None
    pipeline: StagePipelineConfig | None = None
    # Lane-level operator-signoff override (RFC 0007 Phase 3). None defers to
    # OrchestratorConfig.default_signoff. See GitHub #990.
    signoff: Literal["operator"] | None = None
    # Lane-level gate-recipe enablement map (RFC 0009 P4, #1067). Middle tier in
    # resolve_gate_recipe_enabled's 3-tier precedence: consulted when the ticket
    # carries no override for the recipe, and itself overridden by
    # TicketTask.gate_recipes. A recipe absent from this map (or None) defers to
    # the hardcoded default-off. Recognised keys: "auto_approve_clean_review",
    # "auto_adopt_clean_plan".
    gate_recipes: dict[str, bool] | None = None
    # Lane-level review-recipe enablement map (RFC 0010 P3, #1098). Middle tier
    # in resolve_review_recipe_enabled's 3-tier precedence: consulted when the
    # ticket carries no override for the recipe, and itself overridden by
    # TicketTask.review_recipes. A recipe absent from this map (or None) defers
    # to the hardcoded default-off. Recognised keys: "address_review",
    # "auto_fix_ci", "request_reviewer", "escalate_merge_block" (RFC 0010 P4).
    review_recipes: dict[str, bool] | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v:
            msg = "lane name must be non-empty"
            raise ValueError(msg)
        return v

    @field_validator("gate_recipes")
    @classmethod
    def _check_gate_recipes(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_gate_recipe_keys(value)

    @field_validator("review_recipes")
    @classmethod
    def _check_review_recipes(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_review_recipe_keys(value)


_USAGE_LIMIT_BACKOFF_SECONDS = 3600

# RFC 0008 W3 (#1002) default operator-attention forward-set. task.transition
# is admitted only for the terminal/attention-worthy statuses below (narrowed
# further in OperatorChannelForward._admits by cw.cw_operator_events); the
# other four types are unconditional once present in event_types.
_DEFAULT_OPERATOR_EVENT_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.TASK_TRANSITION,
        OrchestratorEventType.TASK_DELETED,
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        OrchestratorEventType.PR_REGISTERED,
        OrchestratorEventType.PR_CI_FAILED,
        OrchestratorEventType.PR_REVIEW_RECEIVED,
        OrchestratorEventType.PR_MERGEABLE,
        OrchestratorEventType.PR_MERGED,
        OrchestratorEventType.SESSION_LIVENESS_CHANGED,
        # RFC 0008 capstone (#1015, Q3): OPERATOR_ESCALATION is the durable
        # escalation latch's operator-facing signal — forwarded by default.
        # CONCIERGE_RECOVERED is deliberately EXCLUDED here: it is an
        # audit-trail record of a *mechanical* (non-destructive) recovery the
        # operator does not need paged for, recorded via record_event but
        # never added to this forward-set.
        OrchestratorEventType.OPERATOR_ESCALATION,
        # RFC 0009 P1+P2 (#1065): a gate recipe auto-approving a review with no
        # human in the loop is operator-attention-worthy — forwarded by default
        # (contrast CONCIERGE_RECOVERED, excluded above as audit-only).
        OrchestratorEventType.GATE_AUTO_APPROVED,
        # Forwarded alongside GATE_AUTO_APPROVED: without this, a failed
        # act-phase mutation would leave GATE_AUTO_APPROVED standing alone on
        # the operator channel as an uncorrected false-positive "approved"
        # signal.
        OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
        # RFC 0010 P2 (#1097): a review recipe dispatching an /address-review
        # action with no human in the loop is operator-attention-worthy —
        # forwarded by default (contrast CONCIERGE_RECOVERED, excluded above as
        # audit-only). PR_ACTION_FAILED forwards alongside so a failed dispatch
        # never leaves PR_ACTION_TAKEN standing alone as an uncorrected signal.
        OrchestratorEventType.PR_ACTION_TAKEN,
        OrchestratorEventType.PR_ACTION_FAILED,
    }
)
_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
    }
)


class OperatorChannelForward(BaseModel):
    """Declarative forward-set for the cw-operator SSE channel (RFC 0008 W3).

    Consumed by ``cw.cw_operator_events``'s filter engine, which additionally
    applies the two sub-condition rules referenced above (task.transition's
    ``new_status`` and session.liveness_changed's ``new_bucket`` are compared
    against ``task_transition_statuses``/``liveness_min_bucket`` respectively;
    every other admitted type in ``event_types`` forwards unconditionally).
    No coercion validator by design -- see the field docstring on
    ``OrchestratorConfig.operator_channel_forward``. See GitHub #1002.
    """

    model_config = ConfigDict(extra="forbid")

    event_types: frozenset[OrchestratorEventType] = Field(
        default_factory=lambda: frozenset(_DEFAULT_OPERATOR_EVENT_TYPES)
    )
    task_transition_statuses: frozenset[QueueItemStatus] = Field(
        default_factory=lambda: frozenset(_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES)
    )
    liveness_min_bucket: LivenessBucket = LivenessBucket.STALE_30M


class OrchestratorConfig(BaseModel):
    """Parsed contents of orchestrator.yaml.

    ``default_max_parallel`` is the cap applied to any client missing from
    ``per_client_max_parallel``. The legacy yaml layout placed this value
    under ``per_client_max_parallel.default``, but that key was treated as
    a literal client name and silently ignored (see GitHub issue #145).
    A model validator migrates any stray ``default`` key into the new
    top-level field so old configs keep working with a one-time warning.
    """

    model_config = ConfigDict(extra="forbid")

    tick_interval_seconds: int = 30
    usage_limit_backoff_seconds: int = _USAGE_LIMIT_BACKOFF_SECONDS
    per_client_max_parallel: dict[str, int] = Field(default_factory=dict)
    default_max_parallel: int = 1
    linear_prefix_map: dict[str, str] = Field(default_factory=dict)
    # Per-tier wall-clock budgets (seconds) for headless DAEMON sessions.
    # Keyed by scope.tier from the auto-dev sentinel; unknown tiers fall back
    # to HEADLESS_TIMEOUT_SECONDS. See GitHub issue #265.
    headless_timeout_by_tier: dict[str, int] = Field(
        default_factory=lambda: {"small": 1800, "large": 5400}
    )
    # Per-stage wall-clock budgets (seconds) for headless DAEMON sessions,
    # consulted BEFORE headless_timeout_by_tier above. Keyed by Stage; a stage
    # absent from this dict (e.g. HARDEN, a dormant stage) falls through to
    # the per-tier default / global HEADLESS_TIMEOUT_SECONDS fallback
    # unchanged. Seeds derived from empirical p99/max wall-clock baselines —
    # finalize legitimately blocks on CI and was falsely parked under the
    # small-tier 1800s floor during the RFC 0007 wave. See GitHub issue #1020.
    # Why: PLAN (3600) and IMPL (4200) are numerically below the large-tier
    # default (5400) this map overrides for those stages on a large-tier
    # ticket. This is intentional, not a regression: each seed clears its
    # own stage's *observed max* wall-clock duration (not the tier default)
    # with a 25-39% margin, and the #976 liveness veto (stalled.py) still
    # backstops any session actively producing output past budget. Full
    # override (no max() composition with the tier map) was a deliberate
    # choice — see ticket #1020 pre-flight resolution §3.
    headless_timeout_by_stage: dict[Stage, int] = Field(
        default_factory=lambda: {
            Stage.PLAN: 3600,
            Stage.IMPL: 4200,
            Stage.REVIEW: 7200,
            Stage.FINALIZE: 5400,
        }
    )
    # Per-tier idle-watchdog budgets (seconds). Keyed by TicketTask.scope_hint;
    # unknown tiers fall back to IDLE_WATCHDOG_SECONDS (900s). Large-tier
    # sessions can legitimately stall longer on slow tests/mypy before emitting
    # any sentinel. 60 min (was 30) shrinks the Mode-3 busy-in-tool-call
    # false-idle window: a real 31-min FINALIZE gate run (pytest+mypy) left no
    # margin and got parked mid-completion. See GitHub issues #326, #340, #918.
    idle_watchdog_by_tier: dict[str, int] = Field(
        default_factory=lambda: {"large": 3600}
    )
    # Global idle-watchdog budget (seconds) applied when a session has no
    # per-ticket override and no resolvable per-tier budget (e.g. it stalled
    # before Stage 1 set a scope_hint). ``None`` falls back to the
    # IDLE_WATCHDOG_SECONDS constant (900s). Raise this so the watchdog does
    # not reap workers still mid-plan/mid-review — 15 min is too short for a
    # full plan+review pass. See the 2026-05-30 fanout-cascade RCA.
    idle_watchdog_seconds: int | None = None
    # Per-tier cap on idle-stall auto-retries before a headless worker is
    # parked BLOCKED_ON_USER for the operator. Keyed by TicketTask.scope_hint;
    # unknown tiers fall back to DEFAULT_IDLE_RETRY_CAP. See GitHub issue #384.
    idle_retry_cap_by_tier: dict[str, int] = Field(default_factory=dict)
    # Per-tier cap on wall-clock-budget (stalled stage) retries before a
    # headless worker is parked BLOCKED_ON_USER instead of re-queued to PENDING.
    # Keyed by TicketTask.scope_hint; unknown tiers fall back to
    # DEFAULT_STALLED_RETRY_CAP. See GitHub issue #756.
    stalled_retry_cap_by_tier: dict[str, int] = Field(default_factory=dict)
    # Absolute ceiling on task.attempts across ALL kill causes. When a task
    # reaches this count in _claim_next_pending, it is parked BLOCKED_ON_USER
    # instead of spawning again. Above the per-stage caps (#756), below the
    # observed 14-attempt usage-limit churn. See GitHub issue #786.
    global_attempt_ceiling: int = DEFAULT_GLOBAL_ATTEMPT_CEILING
    # Consecutive spawn_error count at which a lane's circuit breaker trips and
    # pauses the lane, halting the retry churn a persistent backend outage would
    # otherwise drive. Complements the per-task exponential backoff (#868) and
    # the global attempt ceiling (#786); a paused lane resumes only via
    # ``cw lane resume``. See GitHub issue #875.
    lane_circuit_breaker_threshold: int = 3
    # Number of consecutive failed idle-watchdog observations required before a
    # session is dispositioned (reaped/parked/git-salvaged). 1 reproduces the
    # pre-#545 single-observation behavior. See GitHub #545.
    idle_confirm_observations: int = 2
    # Consecutive per-client freshness-gate-block count at which a
    # session.needs_attention (paused_status="freshness_gate_blocked") is
    # emitted exactly once (latch: no re-fire while still at/above threshold,
    # resets on the next non-stale tick). RFC 0007 §W2.
    freshness_block_attention_threshold: int = 5
    # Consecutive per-session salvage-skip count at which a
    # session.needs_attention (paused_status="salvage_skip_escalated") is
    # emitted exactly once (same latch semantics as
    # freshness_block_attention_threshold above). Closes #974.
    salvage_skip_attention_threshold: int = 5
    # `cw doctor` warns when events/inbox.jsonl exceeds either threshold,
    # suggesting `cw event prune`. Read-only: doctor never mutates the inbox
    # itself. See GitHub #856.
    inbox_size_warn_bytes: int = 5_000_000
    inbox_line_count_warn: int = 15_000
    # Gating policy for destructive reap actions (stop daemon, revert task to
    # PENDING, remove worktree). Default ``signal_only`` routes stalled/phantom
    # sessions to BLOCKED_ON_USER for operator review; ``auto`` restores the
    # pre-#554 self-healing behavior. See ADR-0006 invariant 4 and GitHub #554.
    reap_policy: ReapPolicy = ReapPolicy.SIGNAL_ONLY
    # Elapsed seconds before reconcile attempts to route an emitted-but-unrouted
    # sentinel (signal_stop never fired). Shorter than the idle watchdog budget
    # because an emitted sentinel is positive evidence the worker completed.
    # See GitHub #578.
    sentinel_unrouted_check_seconds: int = 300
    # RFC 0004 Phase 2 — two-knob scheduler (#558)
    # Tier-1: limit how many clients are eligible per tick.
    # None = no limit (today's behavior preserved).
    max_parallel_clients: int | None = None
    # Tier-2: per-client ceiling across all lanes. Takes precedence over the
    # legacy per_client_max_parallel / default_max_parallel fields; those are
    # migrated on load via _migrate_legacy_ceiling_fields and kept as deprecated
    # aliases for one release.
    per_client_ceiling: dict[str, int] = Field(default_factory=dict)
    default_ceiling: int = 1
    # Minimum elapsed seconds between PR-state hydration passes in the serve
    # tick. Gated off max(pr_state.hydrated_at) across tasks (no separate timer
    # state). See GitHub #929.
    pr_hydration_interval_seconds: int = 150
    # Global default for the operator-signoff gate (RFC 0007 Phase 3), used
    # when neither the ticket (TicketTask.signoff) nor its lane
    # (LaneConfig.signoff) sets an override. "none" == no gate (today's
    # behavior); "operator" gates every ticket at the REVIEW->FINALIZE
    # checkpoint pending an explicit ``cw dev-queue approve``.
    # Why no coercion validator (asymmetry with reap_policy): reap_policy has
    # a fail-safe `_coerce_reap_policy` validator because ADR-0006 requires an
    # invalid/missing value to silently degrade to the non-destructive
    # SIGNAL_ONLY default -- a config typo must never accidentally enable
    # destructive auto-reap. default_signoff has the opposite risk profile: a
    # config typo silently coercing to "none" would silently DISABLE an
    # operator's ship gate, which is the one thing this field exists to
    # guarantee. Pydantic's Literal validation already raises loudly on an
    # invalid value, which is the correct fail-closed behavior here.
    default_signoff: Literal["none", "operator"] = "none"
    # RFC 0008 W2 — global ladder of transcript-staleness thresholds (minutes),
    # ordered [stale_15m, stale_30m, stale_45m]. A session's transcript-mtime
    # age is compared against these to classify Session.liveness_bucket.
    # See GitHub #1001.
    liveness_buckets_minutes: list[int] = Field(default_factory=lambda: [15, 30, 45])
    # Per-stage override of the ENTRY-POINT threshold (the effective "floor"
    # below which a session is LIVE) for the liveness ladder above. Keyed by
    # Stage; a stage absent from this dict uses liveness_buckets_minutes[0] as
    # its floor. Raising a stage's floor above a global threshold makes that
    # threshold unreachable for sessions at that stage (labels keep their
    # global-threshold identity; only the entry point moves) — e.g. an IMPL
    # session with floor=35 never emits stale_30m (global threshold 30 < 35).
    # Defaults to IMPL: 35 per the RFC 0008 W2 empirical baselines (impl p99
    # gap 31m vs review p95 9m) — without this default every client config
    # would need a manual override just to avoid spurious stale_15m noise on
    # normal impl-stage idling. See GitHub #1001.
    liveness_first_bucket_by_stage: dict[Stage, int] = Field(
        default_factory=lambda: {Stage.IMPL: 35}
    )
    # RFC 0008 W3 (#1002) — declarative operator-attention forward-set for the
    # cw-operator SSE channel bridge (cw.cw_operator_events). No coercion
    # validator (fail-loud, mirrors default_signoff's asymmetry with
    # _coerce_reap_policy below): under-forwarding is a silent operator-facing
    # regression, so a malformed forward-set must crash `cw queue-channel
    # serve` at startup rather than silently degrade.
    operator_channel_forward: OperatorChannelForward = Field(
        default_factory=OperatorChannelForward
    )
    # RFC 0008 capstone (#1015) — daemon-side mechanical recovery reactor
    # opt-in. Default False: the 3 concierge recipes in cw.reconcile.concierge
    # requeue/restore tasks in ways adjacent to ADR-0006's destructive-action
    # gate (reap_policy), so nothing fires without an explicit operator
    # opt-in, mirroring reap_policy's own fail-safe default. See
    # docs/dispatch-runbook.md "Concierge & Watchdog" and
    # config/CONFIG_REFERENCE.md (Q1).
    concierge_enabled: bool = False
    # Per-recipe enable/disable, merged onto
    # cw.reconcile.concierge.DEFAULT_CONCIERGE_RECOVERIES (all True) via
    # cw.reconcile.concierge.resolve_concierge_recipe_enabled — NOT a
    # full-replace map (Q7). An operator setting one recipe key must not
    # silently disable the other two. Recognised keys: "false_park_requeue",
    # "park_marker_poison_clear", "cancelled_row_restore".
    concierge_recoveries: dict[str, bool] = Field(default_factory=dict)
    # RFC 0009 P1+P2 (#1065) — gate-recipe automation master switch. Default
    # False: the auto_approve_clean_review recipe in cw.reconcile.gate_recipes
    # approves a review gate with NO human review, so nothing fires without an
    # explicit operator opt-in — mirroring concierge_enabled's fail-safe
    # default. Per-recipe / per-lane resolution (LaneConfig.gate_recipes,
    # resolve_gate_recipe_enabled) is deferred to #1067.
    gate_recipes_enabled: bool = False
    # RFC 0010 P1 (#1096) — review-recipe automation master switch (detect
    # phase only in P1; no act phase exists yet, so True is inert by
    # construction until P2 ships). Default False, mirroring
    # gate_recipes_enabled's fail-safe default.
    review_recipes_enabled: bool = False
    # Tool-name patterns forwarded to EVERY DAEMON worker spawn as a single
    # `--disallowed-tools=<comma-joined>` token (cw.spawn.build_disallowed_tools_arg).
    # Default empty: cw forces no tool restriction on workers. Replaces the
    # former hard-coded, tracker-gated Linear-MCP block (#726) — restricting an
    # MCP whose headless auth behaves badly is the operator's policy to set
    # here, not cw's to impose from a tracker heuristic. Patterns use claude's
    # `--disallowed-tools` glob syntax, e.g. "mcp__plugin_linear_linear__*".
    disallowed_mcp_tools: list[str] = Field(default_factory=list)

    @field_validator("disallowed_mcp_tools")
    @classmethod
    def _validate_disallowed_mcp_tools(cls, value: list[str]) -> list[str]:
        """Reject blank/whitespace-only patterns (fail-loud, not silent-drop).

        A blank entry would render as an empty comma-field in the
        `--disallowed-tools=` value and silently weaken the restriction the
        operator intended — the same fail-closed reasoning as default_signoff.
        Pydantic already enforces ``list[str]``; this adds the
        non-empty-element guard.
        """
        for pattern in value:
            if not pattern.strip():
                msg = (
                    "disallowed_mcp_tools entries must be non-empty, non-blank strings"
                )
                raise ValueError(msg)
        return value

    @field_validator("concierge_recoveries")
    @classmethod
    def _validate_concierge_recoveries_keys(
        cls, value: dict[str, bool]
    ) -> dict[str, bool]:
        """Fail loud on an unrecognized recipe key (Q7's guarantee, part 2).

        Local literal, not an import of
        cw.reconcile.concierge.DEFAULT_CONCIERGE_RECOVERIES — models.py sits
        below cw.reconcile in the import graph (reconcile imports from
        models, not the reverse), so importing it here would be circular.
        Without this check, a typo'd key (e.g. "flase_park_requeue") would
        silently no-op via resolve_concierge_recipe_enabled's plain .get()
        fallback, leaving the intended recipe running with zero error at
        config-load time — exactly the silent-misconfiguration failure mode
        operator_channel_forward's own fail-loud stance (see its docstring
        above) already treats as unacceptable for this kind of operator-facing
        config surface.
        """
        recognized = {
            "false_park_requeue",
            "park_marker_poison_clear",
            "cancelled_row_restore",
        }
        unknown = sorted(set(value) - recognized)
        if unknown:
            msg = (
                f"concierge_recoveries has unrecognized recipe key(s): {unknown}. "
                f"Recognised keys: {sorted(recognized)}."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_ceiling_fields(cls, data: object) -> object:
        """Lift legacy per_client_max_parallel / default_max_parallel into new fields.

        The new per_client_ceiling / default_ceiling fields take precedence when
        both are present. Legacy fields are kept as deprecated aliases and still
        populate OrchestratorConfig for one release — callers using the legacy
        field name directly will see the same value via the new field.
        """
        if not isinstance(data, dict):
            return data
        has_new_ceiling = "per_client_ceiling" in data or "default_ceiling" in data
        legacy_per_client = data.get("per_client_max_parallel")
        legacy_default = data.get("default_max_parallel")
        if not has_new_ceiling:
            if isinstance(legacy_per_client, dict) and legacy_per_client:
                data.setdefault("per_client_ceiling", dict(legacy_per_client))
                logging.getLogger(__name__).warning(
                    "OrchestratorConfig: per_client_max_parallel is deprecated; "
                    "use per_client_ceiling instead"
                )
            if isinstance(legacy_default, int):
                data.setdefault("default_ceiling", legacy_default)
                logging.getLogger(__name__).warning(
                    "OrchestratorConfig: default_max_parallel is deprecated; "
                    "use default_ceiling instead"
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _coerce_reap_policy(cls, data: object) -> object:
        """Coerce invalid/absent reap_policy to signal_only (fail-safe, ADR-0006)."""
        if not isinstance(data, dict):
            return data
        val = data.get("reap_policy")
        if not isinstance(val, str) or val not in {p.value for p in ReapPolicy}:
            data["reap_policy"] = ReapPolicy.SIGNAL_ONLY
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_default_key(cls, data: object) -> object:
        """Lift a stray ``per_client_max_parallel.default`` into the top field.

        Only fires when the caller has not already set ``default_max_parallel``
        explicitly — explicit configuration wins. The legacy key is removed
        from the per-client dict so it doesn't shadow real client names.
        """
        if not isinstance(data, dict):
            return data
        per_client = data.get("per_client_max_parallel")
        if not isinstance(per_client, dict):
            return data
        legacy = per_client.pop("default", None)
        if legacy is None:
            return data
        if "default_max_parallel" not in data:
            data["default_max_parallel"] = legacy
        return data


class HookRule(BaseModel):
    """A user-defined shell command to run when a lifecycle event fires."""

    event_type: str
    command: str
    description: str = ""


class EventHookRegistry(BaseModel):
    """Persisted event hook rules for a client."""

    rules: list[HookRule] = Field(default_factory=list)


class LocalLivenessHandle(BaseModel):
    """Process-liveness handle for a LocalExecutor aider subprocess (RFC 0005 F3).

    Binds a PID to its process creation-time (nanoseconds, epoch-relative —
    ``psutil.Process(pid).create_time()``, see GitHub #921) captured at spawn.
    The start-time pin lets harvest detection reject a recycled PID: a dead
    aider PID reassigned to an unrelated process re-reads a different
    start-time, so the session is treated as dead (harvested) rather than
    falsely observed alive. Frozen — an immutable snapshot. See GitHub #888.
    """

    model_config = ConfigDict(frozen=True)

    pid: int
    start_time_ns: int


class Session(BaseModel):
    """A tracked Claude Code session."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    name: str  # Human-readable: "client-a/impl"
    client: str
    purpose: SessionPurpose
    status: SessionStatus = SessionStatus.ACTIVE
    origin: SessionOrigin = SessionOrigin.USER
    workspace_path: Path
    worktree_path: Path | None = None
    branch: str | None = None
    surface_ref: str | None = None
    claude_session_id: str | None = None
    auto_backgrounded: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    idle_at: datetime | None = None
    # Consecutive idle-watchdog observations where this session failed liveness
    # checks; reset on recovery; session is dispositioned (reaped/parked/
    # git-salvaged) only when it reaches OrchestratorConfig.idle_confirm_observations.
    # See GitHub #545.
    idle_observation_count: int = 0
    # Consecutive salvage-skip count for the per-session attention latch
    # (closes #974). Incremented each time this session is skipped via
    # ProposedAction.SKIP_PARKED (SESSION_SALVAGE_SKIPPED); reset to 0 on
    # recovery (any non-SKIP_PARKED detect-phase disposition). Same
    # reset-on-recovery latch shape as idle_observation_count above.
    consecutive_salvage_skips: int = 0
    backgrounded_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_reason: CompletionReason | None = None
    completed_at: datetime | None = None
    # Reason written at each reap site so the queue-events bus server can
    # include it in queue.session_reaped notifications. Finer-grained than
    # CompletionReason — see ReapReason and GitHub #380. None for sessions
    # not reaped by reconcile (e.g. user-backgrounded or /session-done'd).
    reap_reason: ReapReason | None = None
    # Stamped in-place (under sessions_lock, NOT via mutate_state — self-deadlock
    # risk per ADR-0006 invariant 2) when SESSION_REAP_PROPOSED is emitted for
    # this session. Dedup guard: _emit_reap_proposed skips sessions already
    # stamped. See GitHub #555.
    reap_proposed_at: datetime | None = None
    # Dispatch lane this session was spawned into. Stamped by spawn_create_impl
    # when called from the dispatch loop (GitHub #594). None for sessions
    # spawned outside the queue (interactive, plan, cli). Stored for
    # observability; occupancy counting remains task-join based (ADR-0006).
    lane: str | None = None
    parent_session_id: str | None = None
    worker_session_ids: list[str] = Field(default_factory=list)
    # Sentinel-block summary parsed from a headless /auto-dev worker's stdout
    # at completion time. ``None`` for any session that didn't run headless or
    # whose stdout could not be parsed. Stored as a raw dict (rather than the
    # AutoDevResult Pydantic model) so the persisted state file remains
    # readable when the result schema bumps independently of cw's CW_STATE
    # schema. See ``cw.auto_dev_result`` for the parser.
    last_result: dict[str, Any] | None = None
    # Total USD cost for this session's auto-dev run. Populated by
    # signal_stop from AutoDevResult.cost_usd. None when cost data
    # was not emitted by the producer. See GitHub issue #124.
    cost_usd: float | None = None
    # Per-model cost breakdown for this session. Populated via the SDK
    # orchestrator path (post-#116). None when not available.
    cost_breakdown: dict[str, float] | None = None
    # RFC 0005 A1 — dormant; tracks which pipeline stage spawned this session.
    # None for sessions not spawned by the staged pipeline (GitHub #612).
    stage: Stage | None = None
    # RFC 0005 F3 — process-liveness handle for a fire-and-forget LocalExecutor
    # aider subprocess. Set when LocalExecutor.spawn() launches aider and leaves
    # the session ACTIVE; reconcile/local harvest reads it to detect the dead
    # process and synthesize the git-based completion. None for every non-LOCAL
    # session (surface_ref-backed sessions use daemon-roster liveness). See #888.
    local_liveness: LocalLivenessHandle | None = None
    # RFC 0008 W2 — latched transcript-staleness bucket, edge-triggered by
    # cw.reconcile.liveness on each crossing (no per-observation counter, unlike
    # idle_observation_count above). Session.stage is NOT used to resolve the
    # per-stage floor; the owning TicketTask.stage is (see
    # cw.reconcile.liveness._detect_liveness_candidates). See GitHub #1001.
    liveness_bucket: LivenessBucket = LivenessBucket.LIVE


DEFAULT_AUTO_PURPOSES: list[SessionPurpose] = [
    SessionPurpose.IDEA,
    SessionPurpose.IMPL,
    SessionPurpose.DEBT,
]


class ClientConfig(BaseModel):
    """Configuration for a client workspace.

    Two modes:
    - **Legacy**: ``workspace_path`` points to an existing clone.
    - **Worktree**: ``repo_path`` + ``branch`` are set.  ``workspace_path``
      is auto-set to ``repo_path`` as a sentinel; the real worktree path is
      resolved at session start time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    # Typed as Path but defaults to None; the model validator below guarantees
    # it is always set after construction (from either the user or repo_path).
    workspace_path: Path = Field(default=None)  # type: ignore[assignment]
    repo_path: Path | None = None
    branch: str | None = None
    default_branch: str = "main"
    # Prefix for the per-ticket feature branch the staged pipeline provisions
    # and the auto-dev skills push to: ``<feature_branch_prefix>/<ticket_id>``
    # (e.g. ``dev/662``). Single source of truth shared by cw's worktree
    # provisioning (dispatch) and the skill's branch_pattern, so cw and the
    # worker agree on one branch — no mid-pipeline rename that would trip the
    # worktree-reuse guard (#712). Distinct from the session-name prefix
    # ``auto-dev/`` (AUTO_DEV_LABEL_PREFIX), which stays for reconcile's
    # ticket-from-session-name parsing.
    feature_branch_prefix: str = "dev"
    worktree_base: Path | None = None
    auto_purposes: list[SessionPurpose] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_PURPOSES),
    )
    purpose_prompts: dict[str, str] = Field(default_factory=dict)
    # When set, ``cw`` passes ``--model <worker_model>`` to ``claude --bg``
    # for DAEMON-origin spawns (auto-dev workers, including resume re-spawns
    # in :func:`cw.session.resume_session`). Opaque string — no validation;
    # user is responsible for matching Anthropic's published model ids.
    # Default ``None`` inherits the user's logged-in default model.
    # See issue #248.
    worker_model: str | None = None
    # RFC 0011 S1 D-S2b — override for the GitHub login used in counterparty
    # (self|external) and self-identity resolution (see
    # cw.operator_identity.resolve_operator_login). Opaque string — no
    # validation. Default None: the runtime-resolved `gh api user` login
    # (cw.gh.current_gh_login, process-lifetime cached) is authoritative.
    # Set this only for the rare multi-account case where the operator's
    # logged-in gh identity differs from the login this client should treat
    # as "self."
    operator_github_login: str | None = None
    auto_background_threshold: int | None = None
    notifications: bool = False
    lanes: list[LaneConfig] = Field(default_factory=list)
    # RFC 0005 A1 — dormant pipeline config; no dispatch wiring yet (#612).
    pipeline: StagePipelineConfig = Field(default_factory=StagePipelineConfig)

    @property
    def effective_lanes(self) -> list[LaneConfig]:
        """Return declared lanes; synthesize a default lane when none are declared."""
        if self.lanes:
            return list(self.lanes)
        return [LaneConfig(name=DEFAULT_LANE)]

    @model_validator(mode="after")
    def _validate_path_config(self) -> ClientConfig:
        has_workspace = self.workspace_path is not None
        has_repo = self.repo_path is not None and self.branch is not None

        if not has_workspace and not has_repo:
            msg = "Either workspace_path or both repo_path + branch must be set"
            raise ValueError(msg)

        if self.repo_path is not None and not has_workspace:
            # Sentinel: real path resolved at start time via create_worktree
            self.workspace_path = self.repo_path

        return self

    @property
    def is_worktree_client(self) -> bool:
        """True when this client uses repo_path + branch (worktree mode)."""
        return self.repo_path is not None and self.branch is not None


class CwState(BaseModel):
    """Persisted state across all sessions."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    schema_version: int = CW_STATE_SCHEMA_VERSION
    sessions: list[Session] = Field(default_factory=list)

    def active_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.ACTIVE]

    def backgrounded_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.BACKGROUNDED]

    def idled_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.status == SessionStatus.IDLE]

    def find_session(self, client: str, purpose: str) -> Session | None:
        """Find the most recent session for a client+purpose combo."""
        matches = [
            s
            for s in self.sessions
            if s.client == client
            and s.purpose == purpose
            and s.status != SessionStatus.COMPLETED
        ]
        if not matches:
            return None
        return max(matches, key=lambda s: s.started_at)

    def find_by_name_or_id(self, identifier: str) -> Session | None:
        """Find a session by name (client/purpose) or ID."""
        for s in reversed(self.sessions):
            if identifier in (s.name, s.id):
                return s
        return None
