"""Orchestrator and lane configuration models.

Depends on ``cw.models.enums`` and ``cw.models.tasks`` (for the shared recipe-key
validators). See ``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cw.models.enums import (
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    Stage,
)
from cw.models.tasks import _validate_gate_recipe_keys, _validate_review_recipe_keys


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


# Absolute ceiling on task.attempts across all kill causes (#786).
# Lives here so OrchestratorConfig.global_attempt_ceiling can reference it
# directly without a circular import (dispatch.py imports from models.py).
DEFAULT_GLOBAL_ATTEMPT_CEILING = 10


CLAUDE_NATIVE_BACKEND: str = "claude-native"
LOCAL_BACKEND: str = "local"
CODEX_BACKEND: str = "codex"
OPENCODE_BACKEND: str = "opencode"

# Relative path of the per-worktree materialized ticket context, shared by
# dispatch's pre-spawn invalidation (#1046) and local_runner's prompt builder
# so the two never drift onto different literal paths for the same file.
CONTEXT_JSON_RELATIVE_PATH: Path = Path(".cw", "context.json")

# Relative path of the per-worktree hook/correlation context written at spawn
# (``spawn._write_hook_context``). Distinct LAYER from CONTEXT_JSON_RELATIVE_PATH
# above: that file is *ticket* context (title/body/comments) materialized by
# Stage 0 and deleted by dispatch's stale-context invalidation (#1046), while
# this one carries *dispatch/session* state (session ids, queue_metadata) and
# survives a rescued respawn. Shared by the writer and by the readers of
# ``queue_metadata`` so a reader can never drift onto the other file's literal
# path — the defect #1730 shipped, where the pending_operator_comment read
# pointed at .cw/context.json and silently always returned False.
HOOK_CONTEXT_RELATIVE_PATH: Path = Path(".claude", "cw-context.json")

# Keys of the ``agent_spawn_stamp`` object inside cw-context.json (#1646).
# Three modules touch this one object across two layers that cannot import
# each other -- ``cw.spawn`` seeds it, ``cw.cli.agent_spawn_stamp``'s
# PreToolUse/PostToolUse pair increments and decrements it, and
# ``cw.reconcile._shared`` reads it during phantom classification. They live
# beside HOOK_CONTEXT_RELATIVE_PATH for exactly the reason its own docstring
# gives: a reader that hand-types the literal is one typo away from silently
# always returning the default, the defect #1730 shipped.
#
# Shape: {"unresolved_count": int, "last_stamped_at": isoformat str | None}.
# A counter rather than a flag because Claude Code can dispatch several
# subagent tool_use blocks in one assistant turn, so two Pre hooks can fire
# before either Post does -- a boolean would lose the second spawn.
AGENT_SPAWN_STAMP_KEY = "agent_spawn_stamp"
AGENT_SPAWN_UNRESOLVED_COUNT_KEY = "unresolved_count"
AGENT_SPAWN_LAST_STAMPED_AT_KEY = "last_stamped_at"


def extract_unresolved_spawn_count(context: dict[str, object]) -> int:
    """Return the ``agent_spawn_stamp`` counter in *context*, or 0 for any odd shape.

    Shared by ``cw.cli.agent_spawn_stamp`` (write side) and
    ``cw.reconcile._shared`` (read side) so the two independent readers of
    this on-disk shape cannot silently drift onto different validation rules
    (#1646 review finding) -- reconcile cannot import ``cw.cli``, so this
    lives here instead, beside the key constants both layers already import.

    A missing/non-dict stamp, a missing count, a non-int count, or a ``bool``
    masquerading as an int (``bool`` is an ``int`` subclass in Python, so
    ``True`` would otherwise read as a live count of 1) all read as 0.
    """
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return 0
    count = stamp.get(AGENT_SPAWN_UNRESOLVED_COUNT_KEY)
    if isinstance(count, bool) or not isinstance(count, int):
        return 0
    return count


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
    # Lane-level proactive finalize-hold override (RFC 0011 A3, #1160). None
    # defers to OrchestratorConfig.default_finalize_gate. Overridden by
    # TicketTask.hold_finalize -- see resolve_hold_finalize
    # (dispatch/review_gates.py, moved from dispatch/routing.py by #1823).
    finalize_gate: Literal["manual"] | None = None
    # Lane-level override for the codex backend's autonomous MUST_FIX fix loop
    # (#1553, superseding the removed ClientConfig.codex_fix_loop_enabled from
    # #1465). None defers to OrchestratorConfig.default_codex_fix_loop_enabled.
    # Resolved by cw.codex_background._resolve_codex_fix_loop_enabled, mirroring
    # resolve_reap_policy's lane-then-global fallthrough shape. Literal[True]
    # (not bool): a lane can only opt IN to the fix loop, never opt a client's
    # global-True default back OUT -- the same asymmetry finalize_gate/signoff
    # already encode for their own gates.
    codex_fix_loop_enabled: Literal[True] | None = None
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
_HOURS_PER_DAY = 24

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
        # RFC 0011 A3 (#1160): forwarded alongside GATE_AUTO_APPROVED for the
        # same reason as GATE_AUTO_APPROVE_FAILED above -- an A3 force hold
        # declining the mutation would otherwise leave GATE_AUTO_APPROVED
        # standing alone on the operator channel as an uncorrected "approved"
        # signal. Declined rather than raised, but the correction is identical.
        OrchestratorEventType.GATE_AUTO_APPROVE_HELD,
        # RFC 0010 P2 (#1097): a review recipe dispatching an /address-review
        # action with no human in the loop is operator-attention-worthy —
        # forwarded by default (contrast CONCIERGE_RECOVERED, excluded above as
        # audit-only). PR_ACTION_FAILED forwards alongside so a failed dispatch
        # never leaves PR_ACTION_TAKEN standing alone as an uncorrected signal.
        OrchestratorEventType.PR_ACTION_TAKEN,
        OrchestratorEventType.PR_ACTION_FAILED,
        # GitHub #1437: the ssh_key_gate operator escape hatch suppressing an
        # already-live safety probe is attention-worthy, same rationale as
        # GATE_AUTO_APPROVED above.
        OrchestratorEventType.SSH_KEY_GATE_BYPASSED,
        # GitHub #1730: a review-stage requeue proceeding with no operator-visible
        # confirmation that the send-back comment actually reached the reviewer is
        # a no-human-in-the-loop decision -- operator-attention-worthy, forwarded
        # by default, same rationale as GATE_AUTO_APPROVED above (contrast
        # CONCIERGE_RECOVERED, excluded as audit-only). No companion "delivery
        # succeeded" event exists to pair this with (see #1730 Decisions item 4)
        # -- this event is self-contained, not a correction to another forwarded
        # signal.
        OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
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
    # RFC 0011 follow-up (#1171) — repo-keyed operator-login override,
    # consulted by cw.operator_identity.resolve_operator_login_for_repo at the
    # client-less entry points that have no ClientConfig to read
    # ClientConfig.operator_github_login from (``cw review register``, the
    # review_requested webhook, hydrate_pr_states). Exact-string "owner/repo"
    # key match (case-sensitive), same as linear_prefix_map's prefix keys and
    # WatchedPr.repo/the _parse_pr_url-derived repo string — no
    # case-normalization exists anywhere else in this precedence chain. No
    # validator: same fail-loud-on-type-mismatch precedent as
    # linear_prefix_map (a non-string value raises ValidationError ->
    # ConfigValidationError at load_orchestrator_config(), same as every
    # other typed dict field here).
    operator_github_login_by_repo: dict[str, str] = Field(default_factory=dict)
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
    # Fixed re-notify interval (minutes) for the recurring lane-starved
    # session.needs_attention signal (#1630). Scope is LANE_CIRCUIT_PAUSED
    # only -- a circuit-paused lane with pending work fires immediately on
    # first detection, then again every N minutes while it stays starved
    # (gated by LaneConcurrencyOverride.lane_starved_notify_next_eligible_at),
    # so an operator without an active `cw dev-queue status` poll still
    # learns pending work is stranded. Fixed, not exponential (contrast
    # freshness_block_attention_threshold below, which is a one-shot latch,
    # and concierge's false_park_recovery backoff, which doubles) -- a
    # starved lane's operator page should recur at a steady cadence until the
    # operator acts, not decay into silence.
    lane_starved_notify_interval_minutes: int = 15
    # Retention window (hours) for per-session executor-diagnostics bundles
    # under state_dir()/sessions/<id>/diagnostics/. dispatch_tick's cleanup
    # pass rmtree's any bundle whose newest file is older than this. See
    # GitHub #1239.
    diagnostics_retention_hours: int = 24
    # Consecutive per-client freshness-gate-block count at which a
    # session.needs_attention (paused_status="freshness_gate_blocked") is
    # emitted exactly once (latch: no re-fire while still at/above threshold,
    # resets on the next non-stale tick). RFC 0007 §W2.
    freshness_block_attention_threshold: int = 5
    # Maximum consecutive sentinel-stage-mismatch vetoes the phantom sweep will
    # grant a single already_refused session before it lets the pending
    # CRASH_COMPLETE fall-through proceed anyway (closes #1449). Deliberately
    # small: this counts ONLY vetoes that fire while the
    # transcript is still LIVE (staleness below TRANSCRIPT_LIVENESS_WINDOW_SECONDS)
    # on a session whose most recent tick refused a stage-mismatched sentinel, so
    # 2 consecutive live vetoes already reproduce the #1281 "would have crashed
    # two sweeps after the refusal" window that motivated this bound. Reset for
    # free per episode via a fresh Session.
    sentinel_mismatch_veto_cap: int = 2
    # RFC 0010 anomaly layer (#1201) — review-recipe repeat-fire burst detector.
    # A review recipe that keeps firing on the same PR across successive
    # attention_state episodes without the PR ever clearing is thrashing.
    # review_recipe_repeat_fire_threshold is the count of PR_ACTION_TAKEN events
    # for a single (ticket_id, recipe) within
    # review_recipe_repeat_fire_window_minutes at which one
    # session.needs_attention (paused_status="review_recipe_repeat_fire") is
    # emitted — on the exact crossing only (no re-fire once past it). Consumed
    # solely by cw.reconcile.review_recipes' burst detector; the sibling
    # liveness doctor check (#1201) needs no config field.
    review_recipe_repeat_fire_threshold: int = 5
    review_recipe_repeat_fire_window_minutes: int = 20
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
    # sentinel (signal_stop never fired). A re-check delay, not a disposition
    # timer: an emitted sentinel is positive evidence the worker completed.
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
    # GitHub #1444 — host-capacity admission gate. Fleet-wide ceiling on
    # concurrent DAEMON sessions, independent of (and folded into) the
    # per-client ceiling above. None = feature off, byte-identical to
    # pre-#1444 behavior.
    host_session_budget: int | None = None
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
    # Global default for the proactive finalize hold (RFC 0011 A3), used when
    # neither the ticket (TicketTask.hold_finalize) nor its lane
    # (LaneConfig.finalize_gate) sets an override. "auto" == no hold (today's
    # behavior); "manual" stops every ticket at the REVIEW->FINALIZE checkpoint
    # with disposition `finalize_gate_held`, released by an explicit
    # ``cw dev-queue approve``.
    # Why no coercion validator: same asymmetry with reap_policy that
    # default_signoff documents above -- a config typo silently coercing to
    # "auto" would silently DISABLE an operator's ship gate, the one thing this
    # field exists to guarantee. Pydantic's Literal validation already raises
    # loudly on an invalid value, which is the correct fail-closed behavior.
    default_finalize_gate: Literal["auto", "manual"] = "auto"
    # Global default for the codex backend's autonomous MUST_FIX fix loop
    # (#1553), used when the ticket's lane (LaneConfig.codex_fix_loop_enabled)
    # sets no override. Default False, mirroring gate_recipes_enabled's and
    # concierge_enabled's fail-safe defaults: enabling `review: {backend:
    # codex}` must not implicitly enable autonomous fix commits. Superseded
    # the removed ClientConfig.codex_fix_loop_enabled (#1465) with a 2-tier
    # (lane -> global) resolver -- see
    # cw.codex_background._resolve_codex_fix_loop_enabled.
    default_codex_fix_loop_enabled: bool = False
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
    # RFC 0008 W2 re-fire cadence (#1858) — fixed interval (minutes) on which
    # the top-bucket operator distress signal (SESSION_NEEDS_ATTENTION) re-fires
    # while a session stays latched at STALE_45M with no bucket crossing.
    # Mirrors lane_starved_notify_interval_minutes's fixed-interval shape
    # (#1630) rather than an exponential backoff -- the operator wants "page me
    # again in N minutes while this is still stuck," not a growing delay.
    liveness_attention_renotify_interval_minutes: int = 60
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
    # GitHub #1437 — operator escape hatch for the SSH-agent-key preflight
    # gate (#927). Default True (gate stays enforced): unlike
    # concierge_enabled/gate_recipes_enabled above, this does NOT gate new
    # automation -- it gates an already-live safety probe that holds the
    # fleet PENDING rather than risk a guaranteed-failing spawn. Setting this
    # False bypasses that skip fleet-wide when the probe reports unavailable;
    # each bypass emits SSH_KEY_GATE_BYPASSED (forwarded to the operator
    # channel by default -- see _DEFAULT_OPERATOR_EVENT_TYPES above).
    ssh_key_gate_enabled: bool = True
    # GitHub #1862 — operator escape hatch for the pre-dispatch open-PR gate
    # (cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids). Default True (gate
    # stays enforced), mirroring ssh_key_gate_enabled's fail-safe default: it
    # gates an already-live probe that can park PLAN/IMPL-stage PENDING tasks,
    # not new automation. Setting this False skips the gate entirely for every
    # client -- the operator's escape hatch if a `gh`-probe fan-out ever stalls
    # a dispatch tick (e.g. a large cold-cache PLAN/IMPL backlog).
    pr_gate_enabled: bool = True
    # Tool-name patterns forwarded to EVERY DAEMON worker spawn as a single
    # `--disallowed-tools=<comma-joined>` token (cw.spawn.build_disallowed_tools_arg).
    # Default empty: cw forces no tool restriction on workers. Replaces the
    # former hard-coded, tracker-gated Linear-MCP block (#726) — restricting an
    # MCP whose headless auth behaves badly is the operator's policy to set
    # here, not cw's to impose from a tracker heuristic. Patterns use claude's
    # `--disallowed-tools` glob syntax, e.g. "mcp__plugin_linear_linear__*".
    # Global by design (no per-lane/per-client override): the operator sets one
    # fleet-wide policy. The removed #726 heuristic's per-client (tracker)
    # scoping was dropped deliberately, not overlooked — a mixed fleet that
    # needs the block on only some clients sets the one pattern that is safe
    # fleet-wide (headless Linear OAuth stalls the same way on every client).
    disallowed_mcp_tools: list[str] = Field(default_factory=list)
    # RFC 0011 A6 (#1162) — the digest delivery window is a LOCAL wall-clock
    # preference, not a UTC timestamp: an operator's wake/sleep hours don't move
    # with DST, and storing them as UTC-hour integers would either page at the
    # wrong local hour after a DST transition or (for a window that crosses UTC
    # midnight, e.g. 08:00-20:00 EDT == 12:00-00:00 UTC) fail to open at all under
    # a naive start<=hour<end comparison. zoneinfo is stdlib (requires-python
    # >=3.13) so this costs no new dependency -- only the DST test coverage below.
    # The timezone is itself a config field, not a hardcoded constant, so a
    # relocating (or future) operator changes config, not code.
    attention_digest_window_tz: str = "America/New_York"
    attention_digest_window_start_hour: int = 8  # local to attention_digest_window_tz
    attention_digest_window_end_hour: int = 20  # local to attention_digest_window_tz
    # Idle-drain floor (seconds): a held event's age must exceed this before a
    # flush inside the window is allowed. Prevents flushing a digest of one
    # immediately after the very first held park of the window/night arrives --
    # the floor gives a second (or third) held park a chance to land before the
    # first digest goes out. See RFC 0011 A6 resolution 5.
    attention_digest_idle_floor_seconds: int = 60

    @field_validator("disallowed_mcp_tools")
    @classmethod
    def _validate_disallowed_mcp_tools(cls, value: list[str]) -> list[str]:
        """Reject blank or comma-bearing patterns (fail-loud, not silent-drop).

        Two silent-corruption modes are guarded, both producing a restriction
        that differs from what the operator wrote with no error raised: a blank
        entry renders as an empty comma-field in the `--disallowed-tools=`
        value, and a comma-bearing entry splits into two patterns when
        ``build_disallowed_tools_arg`` comma-joins the list into one token.
        Same fail-closed reasoning as default_signoff. Pydantic already
        enforces ``list[str]``; this adds the element-shape guard.
        """
        for pattern in value:
            if not pattern.strip():
                msg = (
                    "disallowed_mcp_tools entries must be non-empty, non-blank strings"
                )
                raise ValueError(msg)
            if "," in pattern:
                msg = (
                    "disallowed_mcp_tools entries must not contain ',' (the "
                    "comma-join delimiter); use one list entry per pattern"
                )
                raise ValueError(msg)
        return value

    @field_validator("attention_digest_window_tz")
    @classmethod
    def _validate_attention_digest_window_tz(cls, value: str) -> str:
        """Fail loud on an unresolvable IANA zone (fail-loud, mirrors default_signoff).

        A silent fallback to UTC here reproduces exactly the 4am-page failure
        this field exists to prevent -- an operator who mistypes their zone must
        see a config-load error, not a digest window that silently opens at the
        wrong local hour. Mirrors _validate_disallowed_mcp_tools's raise-on-bad-
        value shape.
        """
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            msg = f"attention_digest_window_tz: unknown IANA zone {value!r}"
            raise ValueError(msg) from None
        return value

    @model_validator(mode="after")
    def _validate_attention_digest_window_hours(self) -> OrchestratorConfig:
        """Fail loud on a start/end pair that can never open (fail-loud, same
        reasoning as ``_validate_attention_digest_window_tz``).

        ``_in_delivery_window`` (``cw.cw_operator_events``) compares
        ``start_hour <= local_hour < end_hour``. Unlike a UTC-hour design, this
        field pair intentionally does not support an overnight wraparound (see
        the field's own why-comment above) -- so a config with
        ``start_hour >= end_hour`` isn't an alternate valid shape, it is a typo
        that makes the predicate false for every hour of every day, forever.
        The digest would then buffer every held ticket and never flush it,
        silently -- exactly the missed-signal failure R8 exists to prevent.
        """
        start, end = (
            self.attention_digest_window_start_hour,
            self.attention_digest_window_end_hour,
        )
        if not (0 <= start < end <= _HOURS_PER_DAY):
            msg = (
                "attention_digest_window_start_hour/end_hour must satisfy "
                f"0 <= start < end <= 24 (got start={start}, end={end}) -- a "
                "start >= end window can never open and would silently drop "
                "every digest"
            )
            raise ValueError(msg)
        return self

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

    # Config keys removed with the process-kill timeouts. Stripped (with a
    # one-time warning) rather than rejected so an operator's existing
    # orchestrator.yaml keeps loading under extra="forbid" — a stale timeout
    # knob must degrade to "no timeout", never to a config-load crash.
    _REMOVED_TIMEOUT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "headless_timeout_by_tier",
            "headless_timeout_by_stage",
            "idle_watchdog_by_tier",
            "idle_watchdog_by_stage",
            "idle_watchdog_seconds",
            "idle_retry_cap_by_tier",
            "stalled_retry_cap_by_tier",
            "idle_confirm_observations",
            "park_veto_cap",
            "salvage_skip_attention_threshold",
        }
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_removed_timeout_fields(cls, data: object) -> object:
        """Drop config keys for the removed process-kill timeouts (warn once).

        The wall-clock budget and idle-watchdog machinery no longer exists;
        these keys have no effect. Stripping keeps old configs loading; the
        warning tells the operator the knob is gone so the config can be
        cleaned up.
        """
        if not isinstance(data, dict):
            return data
        present = sorted(cls._REMOVED_TIMEOUT_KEYS & set(data))
        for key in present:
            data.pop(key)
        if present:
            logging.getLogger(__name__).warning(
                "OrchestratorConfig: ignoring removed timeout setting(s) %s — "
                "process-kill timeouts were removed; sessions are never "
                "dispositioned on elapsed time",
                present,
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
