"""Parsed orchestrator.yaml: ``OrchestratorConfig``.

Depends on ``cw.models.orchestrator_config.constants`` (config defaults and the
pinned logger name), ``cw.models.orchestrator_config.operator_forward`` and
``cw.models.enums``. The validators log under :data:`_LOGGER_NAME`, the
pre-split module name, never ``__name__``.
"""

from __future__ import annotations

import logging
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cw.models.enums import ReapPolicy, Stage
from cw.models.orchestrator_config.constants import (
    _LOGGER_NAME,
    DEFAULT_DISK_PRESSURE_MIN_FREE_GB,
    DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION,
    DEFAULT_DISK_PRESSURE_MIN_FREE_INODES,
    DEFAULT_GLOBAL_ATTEMPT_CEILING,
)
from cw.models.orchestrator_config.operator_forward import OperatorChannelForward

_USAGE_LIMIT_BACKOFF_SECONDS = 3600
_HOURS_PER_DAY = 24


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
    # Fixed interval (minutes) for the dispatch loop's proactive staleness
    # watchdog (#1875). Serves TWO purposes on purpose, not two knobs:
    #
    #   1. the per-client re-notify debounce for
    #      session.needs_attention(paused_status="dispatch_loop_stale"),
    #      gated by ClientConcurrencyOverride.
    #      dispatch_stale_notify_next_eligible_at; and
    #   2. the minimum interval between the watchdog's own inbox SCANS
    #      (cw.dispatch.loop._run_stale_client_watchdog_guarded).
    #
    # (2) exists because the scan calls latest_tick_summary_by_client(),
    # which reads and parses the ENTIRE events inbox -- read_events' since_ts
    # filter shrinks the returned list, not the read/parse cost -- so running
    # it on every 30s tick against an unrotated inbox is real hot-loop cost.
    # A separate scan-interval knob would only let an operator set the two
    # inconsistently: scanning more often than the debounce cannot produce an
    # extra page, and scanning less often would silently cap the page rate
    # below the configured debounce.
    dispatch_stale_notify_interval_minutes: int = 15
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
    # small: this counts vetoes on a session whose most recent tick refused a
    # stage-mismatched sentinel, so 2 consecutive vetoes already reproduce the
    # #1281 "would have crashed two sweeps after the refusal" window that
    # motivated this bound. Since #2405 (ADR-0014 audit), this cap is the veto's
    # only gate -- transcript staleness is a diagnostic, never a condition.
    # Reset for free per episode via a fresh Session.
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
    # `cw doctor` warns (advisory, never fails the exit code) when sessions.json
    # exceeds this many bytes, suggesting `cw session prune` (#1999, retention
    # from #1983). Read-only: stat-only, and nothing runs prune automatically.
    # The 15 MB default comes from ~3.7 KB/session on a long-lived host whose
    # retained set after a prune is ~10 MB. `cw session prune` keeps live and
    # dev-queue-referenced sessions, so raise this field if the nudge persists
    # after a prune. Do NOT tie it to inbox_size_warn_bytes above.
    sessions_size_warn_bytes: int = 15_000_000
    # Auto-prune trigger (#1980): checked in record_event's append path, under
    # _inbox_lock, using the byte size already available from the append write
    # (no extra read). Distinct from inbox_size_warn_bytes/inbox_line_count_warn
    # above (#856), which are doctor-only warnings and never mutate the inbox.
    #
    # event_inbox_retention_bytes intentionally shares its default with
    # inbox_size_warn_bytes above (both 5_000_000) so auto-prune fires at the
    # exact point the doctor would otherwise have warned -- the doctor check
    # becomes a backstop that only fires when auto-prune is disabled or broken.
    # Do NOT derive this value from inbox_size_warn_bytes in code: they are two
    # independent fields that happen to share a default, kept separate so a
    # future reader who changes one field is prompted to consider the other.
    # Defaults are the operator-approved binding values from GitHub #1980's
    # "Ambiguity Resolutions — round 1" comment (Q1).
    event_inbox_auto_prune_enabled: bool = True
    event_inbox_retention_bytes: int = 5_000_000
    event_inbox_retention_count: int = 2000

    @model_validator(mode="after")
    def _validate_event_inbox_retention_ratio(self) -> OrchestratorConfig:
        """Warn (never fail) when the byte trigger can't outlive its own prune.

        If event_inbox_retention_bytes is smaller than the retained events'
        plausible footprint, every append immediately re-crosses the
        threshold after a prune, turning the amortized-O(1)-per-append
        auto-prune into an O(current size)-per-append thrash. 50 bytes/event
        is a deliberately conservative floor -- the ticket's own measured
        density is ~415 bytes/event (GitHub #1980) -- so this only fires on
        configs that can't possibly hold event_inbox_retention_count events,
        not on merely-aggressive ones. Warn rather than raise: tests
        deliberately construct tiny thresholds (e.g. 50 bytes) to force a
        prune on every append, a valid and supported use, not a
        misconfiguration.
        """
        min_plausible_bytes = self.event_inbox_retention_count * 50
        if (
            self.event_inbox_auto_prune_enabled
            and self.event_inbox_retention_bytes < min_plausible_bytes
        ):
            logging.getLogger(_LOGGER_NAME).warning(
                "OrchestratorConfig: event_inbox_retention_bytes=%d is "
                "smaller than event_inbox_retention_count=%d's plausible "
                "footprint (%d bytes) -- auto-prune may thrash on every "
                "append",
                self.event_inbox_retention_bytes,
                self.event_inbox_retention_count,
                min_plausible_bytes,
            )
        return self

    # Gating policy for destructive reap actions (stop daemon, revert task to
    # PENDING, remove worktree). Default ``signal_only`` routes stalled/phantom
    # sessions to BLOCKED_ON_USER for operator review; ``auto`` restores the
    # pre-#554 self-healing behavior. See ADR-0006 invariant 4 and GitHub #554.
    reap_policy: ReapPolicy = ReapPolicy.SIGNAL_ONLY
    # Global defaults for the `cw guard-busy-wait` PreToolUse guard (#1946),
    # overridable per lane (LaneConfig.busy_wait_guard_*). Default-ON, unlike
    # reap_policy's fail-safe default: the failure this guard prevents is a
    # worker holding its turn open with no-op Bash polls, which after ADR-0014
    # removed every kill timer keeps the transcript fresh enough that the
    # staleness sweep classifies the spinning worker as LIVE and
    # session.needs_attention never fires (#1944). The guard's own failure
    # mode is bounded the other way -- it fails open on every unexpected
    # shape -- so the asymmetry runs opposite to reap_policy's.
    #
    # Why no coercion validator: the hook wraps its whole classification in a
    # fail-open try/except, so a malformed config raising out of
    # load_orchestrator_config already degrades to "guard does not fire",
    # which is the same non-destructive end state a coercion validator would
    # manufacture -- with the loud ConfigValidationError still reaching every
    # other cw command that reads the same file.
    busy_wait_guard_enabled: bool = True
    # ge=2: repeat_threshold - 1 is the guard's block threshold
    # (_repeat_threshold_tripped); a value <= 1 collapses that to >= 0, which
    # is always true and blocks every non-bare-noop Bash call on its first
    # occurrence -- the opposite of the fail-open design goal.
    busy_wait_guard_repeat_threshold: int = Field(default=3, ge=2)
    busy_wait_guard_window_seconds: int = Field(default=300, ge=1)
    # Global default for the disposition ledger's drift check (#2232),
    # overridable per lane (LaneConfig.disposition_drift_check_enabled).
    # Default-ON for the same reason busy_wait_guard_enabled is, and for the
    # opposite reason to codex_claim_suppression_enabled two fields below: a
    # CHECK is presumed wanted, a FEATURE is presumed unwanted. Turning it off
    # is a deliberate act with a consequence -- the claim tier refuses to arm
    # on any lane where this resolves False (ClaimTierArmingError), because
    # drift-checking is what keeps a stale settle from silently suppressing a
    # re-raised finding once the fuzzy tier is live (ADR-0016).
    disposition_drift_check_enabled: bool = True
    # Global default for the `cw agent-spawn-pre` spawn-shape policy (#2211),
    # overridable per lane (LaneConfig.subagent_spawn_guard_enabled).
    # Default-ON for the same reason as busy_wait_guard_enabled: the failure
    # it prevents is a forked (or unnamed) subagent doing unrostered work cw
    # can neither see nor stop (#2017), and the guard's own failure mode is
    # bounded the other way -- it fails open on every shape it cannot
    # classify, and refuses only an explicitly-named fork or an omitted
    # subagent_type (deny-on-omission shipped in #2211 round 2, once the
    # spawn-site inventory closed).
    subagent_spawn_guard_enabled: bool = True
    # Global default for the `cw background-tool-guard-pre` guard (#2303),
    # overridable per lane (LaneConfig.background_tool_guard_enabled).
    # Default-ON: the failure it prevents is a headless worker
    # backgrounding a pipeline-dependent Bash call or reaching for
    # Monitor, neither of which has a completion-notification path for a
    # headless DAEMON session (ADR-0003's background_tasks tracking
    # covers only the Agent tool's subagent spawn) -- and the guard fails
    # open on every shape it cannot classify.
    background_tool_guard_enabled: bool = True
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
    # sets no override; a lane may override in either direction (True opts in,
    # False opts out of a global True, #2541). Default False, mirroring
    # concierge_enabled's fail-safe default: enabling `review: {backend:
    # codex}` must not implicitly enable autonomous fix commits. Superseded
    # the removed ClientConfig.codex_fix_loop_enabled (#1465) with a 2-tier
    # (lane -> global) resolver -- see
    # cw.codex_background._resolve_codex_fix_loop_enabled.
    default_codex_fix_loop_enabled: bool = False
    # #2633 — consecutive fix cycles that resolve no originally-found MUST_FIX
    # while the diff grows before the fix loop parks fix_loop_diverging. Was a
    # hardcoded 2; 1 now, since a stalled, growing cycle is already the
    # signature of a loop building unplanned code. A lane may override it
    # (LaneConfig.codex_fix_loop_stall_cycles; 2 restores the old tolerance),
    # resolved by cw.codex_background._resolve_codex_fix_loop_stall_cycles.
    # The literal is pinned to codex_fix_loop.divergence._DIVERGENCE_STALL_CYCLES
    # by a lockstep test.
    codex_fix_loop_stall_cycles: int = Field(default=1, ge=1)
    # #2633 — net non-test source lines one fix cycle may add per open
    # MUST_FIX finding before the in-file growth guard parks it
    # codex_fix_growth_budget_exceeded (only when the plan has a `## Files
    # Modified` manifest). No lane override. The literal is pinned to
    # codex_fix_loop.growth.DEFAULT_GROWTH_BUDGET_LINES by a lockstep test.
    codex_fix_loop_growth_budget_lines: int = Field(default=40, ge=1)
    # #2633 — kill switch for the fix loop's heuristic guards: the growth
    # budget (net lines, top-level def count), the lock, state-file and
    # path-constant detectors, and the operator-constraint violation check.
    # It does not cover the constraint prompt section, the clean-start
    # refusal, the staged-set guard, the scope fence, revert guard,
    # sensitive-path guard, hook-failure park or divergence guard. A lane may
    # override in either direction (LaneConfig.codex_fix_loop_growth_guard_enabled),
    # resolved by cw.codex_background._resolve_codex_fix_loop_growth_guard_enabled.
    codex_fix_loop_growth_guard_enabled: bool = True
    # #2210 — master opt-in for the codex review ledger's fuzzy claim-match
    # suppression tier. Default False, mirroring concierge_enabled's
    # fail-safe posture. BOTH this and the task's lane
    # (LaneConfig.codex_review_tiers["claim_suppression"]) must be true for the
    # tier to suppress anything; either one set False is a kill switch. While
    # it is off the tier still MEASURES itself, emitting one
    # review.finding_claim_shadowed event per finding it would have
    # suppressed -- that is the corpus an operator judges before arming a
    # lane. See cw.codex_background._resolve_claim_tier_enabled and ADR-0016.
    codex_claim_suppression_enabled: bool = False
    # RFC 0008 W2 — global ladder of transcript-staleness thresholds (minutes),
    # ordered [stale_15m, stale_30m, stale_45m]. A session's staleness is
    # compared against these to classify Session.liveness_bucket: its
    # transcript-mtime age, or -- for an unobservable DAEMON session with no
    # surface, Claude id, or local handle (#2417) -- its age since started_at.
    # See GitHub #1001.
    liveness_buckets_minutes: list[int] = Field(default_factory=lambda: [15, 30, 45])
    # Per-stage override of the ENTRY-POINT threshold (the effective "floor"
    # below which a session is LIVE) for the liveness ladder above. The floor is
    # also the grace period before an unobservable session's age pages (#2417:
    # REVIEW defaults to 15m, so no new knob). Keyed by
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
    # RFC 0008 W2 re-evaluation cadence (#1858, #2153) — fixed interval
    # (minutes) on which a session latched at STALE_45M with no bucket crossing
    # is re-evaluated for the dead-session page (SESSION_NEEDS_ATTENTION). The
    # page re-fires only when its evidence key changed (paused_status, the
    # staleness-basis transcript timestamp, or the owned row's status), so one
    # death pages once, not once per interval. Fixed-interval shape like
    # lane_starved_notify_interval_minutes (#1630); the name is kept.
    liveness_attention_renotify_interval_minutes: int = 60
    # #2012 — age bound on the liveness sweep's subagent-await suppression.
    # Before this field, an outstanding `agent_spawn_stamp` entry suppressed
    # the SESSION_NEEDS_ATTENTION distress signal unconditionally and forever:
    # "awaiting a subagent" was, by design, treated as definitionally healthy
    # with no expiry, which is why a wedged fix-loop dispatch stayed invisible
    # to an otherwise correctly-armed watchdog. Past this many minutes the
    # suppression lifts and the distress signal fires with the discriminating
    # `fix_loop_await_deadline_exceeded` paused_status.
    #
    # Signal-only, per ADR-0014: exceeding this deadline never dispositions a
    # session, mutates the dev queue, or touches a worktree — it only stops
    # suppressing an operator-facing signal.
    #
    # Practical effect is bounded by whichever liveness_first_bucket_by_stage /
    # liveness_buckets_minutes threshold gates the STALE_45M crossing it is
    # evaluated at: the deadline is only ever consulted for a session already
    # in (or entering) the top staleness bucket. 30m sits comfortably under
    # that 45m floor so the field has real effect out of the box.
    #
    # #2458: also the age bound under which the idle sweep's staged-emit
    # backstop treats an outstanding stamp as background work still draining
    # and holds off routing (cw.reconcile.idle._detect). Past it, the backstop
    # routes the staged result and completes the session -- constructive
    # completion off the worker's own emitted result, not a timeout reap.
    fix_loop_await_deadline_minutes: int = Field(default=30, ge=1)
    # #2012 — total window (seconds) `cw agent-spawn-verify` polls for a fresh
    # subagent transcript before exiting 1. Operator-tunable rather than a code
    # constant because host/load variance (cold model start, contended host,
    # network-mounted worktree) can make a fixed window report a verification
    # failure for a dispatch that was in fact healthy, just slow to write its
    # first transcript. An affected operator raises this without a code change —
    # same rationale as busy_wait_guard_window_seconds above. `--poll-seconds`
    # overrides it for a single invocation. (The auto-dev fix-loop call site is
    # retired as of #2017; the command remains an operator diagnostic.)
    agent_spawn_verify_poll_seconds: int = Field(default=20, ge=1)
    # Polling cadence (seconds) within the window above. `--poll-interval-
    # seconds` overrides it for a single invocation.
    agent_spawn_verify_poll_interval_seconds: int = Field(default=2, ge=1)
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
    # True: the operator is paged for a product or scope question, never for a
    # ticket's size alone, so the two recipes in cw.reconcile.gate_recipes
    # release a Large plan/review gate whose predicate finds no reason a person
    # is needed (forbidden-area touch, operator scope_hint "large", degraded
    # review health, unbound plan draft). Set False to restore manual approval
    # of every Large gate; per-lane / per-ticket opt-out is
    # LaneConfig.gate_recipes / TicketTask.gate_recipes (resolved by
    # resolve_gate_recipe_enabled).
    gate_recipes_enabled: bool = True
    # RFC 0010 P1 (#1096) — review-recipe automation master switch (detect
    # phase only in P1; no act phase exists yet, so True is inert by
    # construction until P2 ships). Default False, mirroring
    # concierge_enabled's fail-safe default.
    review_recipes_enabled: bool = False
    # GitHub #2135 — master switch for the Stop-hook abandoned-exit park.
    # Default False: the park is a state-mutating auto-actor that moves a
    # dev-queue row RUNNING -> BLOCKED_ON_USER off the worker's recorded park
    # marker, so it ships dark and is armed per-lane by an operator —
    # mirroring concierge_enabled's fail-safe default and
    # docs/release-playbook.md's default-off floor for this change class. With
    # this False the Stop hook defers on a sentinel-less exit exactly as it did
    # before #2135, without reading the marker. Per-lane / per-ticket
    # resolution: LaneConfig.park_on_abandoned_exit,
    # TicketTask.park_on_abandoned_exit, resolve_park_on_abandoned_exit_enabled.
    park_on_abandoned_exit_enabled: bool = False
    # GitHub #1437 — operator escape hatch for the SSH-agent-key preflight
    # gate (#927). Default True (gate stays enforced): unlike
    # concierge_enabled above, this does NOT gate new
    # automation -- it gates an already-live safety probe that holds the
    # fleet PENDING rather than risk a guaranteed-failing spawn. Setting this
    # False bypasses that skip fleet-wide when the probe reports unavailable;
    # each bypass emits SSH_KEY_GATE_BYPASSED (forwarded to the operator
    # channel by default -- see _DEFAULT_OPERATOR_EVENT_TYPES above).
    ssh_key_gate_enabled: bool = True
    # GitHub #1887 (split from #1858) — operator escape hatch for the
    # claim-time disk-pressure preflight gate. Default True (gate stays
    # enforced), same already-live-safety-probe posture as
    # ssh_key_gate_enabled above: it gates a `shutil.disk_usage` probe of the
    # client's worktree-base mount that holds that client PENDING rather than
    # risk a session filling an already-tight disk, not new automation.
    # Setting this False bypasses that skip whenever the probe reports
    # pressure; each bypass emits DISK_PRESSURE_GATE_BYPASSED (forwarded to
    # the operator channel by default -- see _DEFAULT_OPERATOR_EVENT_TYPES
    # above).
    disk_pressure_gate_enabled: bool = True
    # Minimum free space (GB) on a client's worktree-base mount before the
    # gate above holds that client PENDING (#1887). See
    # DEFAULT_DISK_PRESSURE_MIN_FREE_GB for why the default is a judgment
    # call rather than a measured threshold.
    disk_pressure_min_free_gb: float = DEFAULT_DISK_PRESSURE_MIN_FREE_GB
    # Inode floor for the same gate (#2470): the effective minimum free-inode
    # count is max(disk_pressure_min_free_inodes,
    # disk_pressure_min_free_inode_fraction * total_inodes). See
    # DEFAULT_DISK_PRESSURE_MIN_FREE_INODES for the rationale.
    disk_pressure_min_free_inodes: float = DEFAULT_DISK_PRESSURE_MIN_FREE_INODES
    disk_pressure_min_free_inode_fraction: float = (
        DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION
    )
    # GitHub #1862 — operator escape hatch for the pre-dispatch open-PR gate
    # (cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids). Default True (gate
    # stays enforced), mirroring ssh_key_gate_enabled's fail-safe default: it
    # gates an already-live probe that can park PLAN/IMPL-stage PENDING tasks,
    # not new automation. Setting this False skips the gate entirely for every
    # client -- the operator's escape hatch if a `gh`-probe fan-out ever stalls
    # a dispatch tick (e.g. a large cold-cache PLAN/IMPL backlog).
    pr_gate_enabled: bool = True
    # GitHub #2396 — operator escape hatch for #2077's pre-claim
    # worktree-occupancy screen (cw.dispatch.claim.resolve_occupied_ticket_ids).
    # Default True (gate stays enforced), mirroring pr_gate_enabled's fail-safe
    # default: it gates an already-live probe (live_home_reason reads cw
    # session state and the daemon roster) that fails a claim closed on any
    # indeterminate read, so a broad misclassification could otherwise
    # silently stop every PENDING task for a client from being claimed with no
    # dispatch_tick failure. Use ClientConfig.occupancy_gate_enabled=False
    # for a staged per-client rollout; setting this False is the explicitly
    # audited fleet-wide emergency control. Either setting skips the pre-claim
    # precompute and falls back to #2077's post-claim
    # WorktreeOccupiedError/HookContextConflictError handling, which still
    # refuses a genuinely occupied worktree, just one claim later.
    occupancy_gate_enabled: bool = True
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
                logging.getLogger(_LOGGER_NAME).warning(
                    "OrchestratorConfig: per_client_max_parallel is deprecated; "
                    "use per_client_ceiling instead"
                )
            if isinstance(legacy_default, int):
                data.setdefault("default_ceiling", legacy_default)
                logging.getLogger(_LOGGER_NAME).warning(
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
            logging.getLogger(_LOGGER_NAME).warning(
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
