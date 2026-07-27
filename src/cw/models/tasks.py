"""Dev-queue task models: TicketTask, DispatchPlan, DevQueueStore.

Depends on ``cw.models.enums`` and ``cw.models.events``. ``WatchedPr`` is
imported (not merely referenced) because ``DevQueueStore.watched_prs`` resolves
at class-build time under ``from __future__ import annotations``. See
``cw.models.__init__`` for the full DAG.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cw.models.enums import QueueItemStatus, Stage
from cw.models.events import PrState, WatchedPr

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
# v17: added TicketTask.auto_fix_ci_fired_at (GitHub #1205) — one-shot
#      latch for the auto_fix_ci review recipe.
# v18: added TicketTask.address_review_fired_at (GitHub #1206) — one-shot
#      latch for the address_review review recipe.
# v19: added TicketTask.last_blocked_result (GitHub #1266) — diagnostic-only
#      field populated by the _route_blocked_result_to_task
#      unrecognized-reason catch-all; lets an operator distinguish "no
#      sentinel yet" from "a rejected sentinel landed this FAILED."
# v20: added TicketTask.cross_repo_override (GitHub #1198) — operator escape
#      hatch that bypasses the cross-repo dispatch guard for one row.
# v21: added TicketTask.stage_high_water (GitHub #1361) — furthest pipeline
#      stage reached across all attempts; monotonic.
# v22: added TicketTask.blocked_reason (GitHub #1511) — the blocker.reason off
#      a well-formed blocked/merge_gate_blocked AutoDevResult, stamped by
#      transition_task_status alongside disposition so it reaches `cw
#      dev-queue tasks` and the attention-event formatter.
DEV_QUEUE_SCHEMA_VERSION = 22
DEFAULT_LANE: str = "default"
DEFAULT_STAGE: Stage = Stage.PLAN


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
    # GitHub #1205 — one-shot latch for the auto_fix_ci review recipe
    # (cw.reconcile.review_recipes). Stamped by _prepare_auto_fix_ci_job when it
    # emits PR_ACTION_TAKEN for a ci_failing PR, so the CI-fix re-dispatch (a
    # full re-enqueue + dispatch tick, not a scoped session — the highest blast
    # radius of the four review-recipe latches) fires exactly once per
    # ci-failing episode rather than every reconcile tick. Cleared by
    # _act_auto_fix_ci's own episode-end sweep (the shared _clear_ended_episodes
    # helper) when the row's pr_state leaves ci_failing (or goes None),
    # re-arming the latch for a genuine future re-entry — mirrors
    # request_reviewer_fired_at above.
    auto_fix_ci_fired_at: datetime | None = None
    # GitHub #1206 — one-shot latch for the address_review review recipe
    # (cw.reconcile.review_recipes). Stamped by _prepare_dispatch_job when it
    # emits PR_ACTION_TAKEN for a changes_requested PR, so the
    # /address-review dispatch fires exactly once per changes-requested
    # episode rather than every reconcile tick. Cleared by
    # _act_address_review's own episode-end sweep (the shared
    # _clear_ended_episodes helper) when the row's pr_state leaves
    # changes_requested (or goes None), re-arming the latch for a genuine
    # future re-entry — mirrors auto_fix_ci_fired_at above.
    address_review_fired_at: datetime | None = None
    # GitHub #1266 -- diagnostic-only field for the _route_blocked_result_to_task
    # unrecognized-reason catch-all: the only FAILED landing that transitioned
    # a task without recording *why*. Deliberately NOT named last_result --
    # that name is Session.last_result's, a distinct, business-critical field
    # (gates cw dev-queue approve) with a different shape/update cadence.
    # TicketTask has none today. Populated exclusively by that one catch-all;
    # every other task keeps last_blocked_result=None. Lets an operator
    # distinguish "sentinel never arrived" from "a rejected sentinel landed
    # this FAILED."
    last_blocked_result: dict[str, Any] | None = None
    # GitHub #1511 — the `blocker.reason` off a well-formed blocked/
    # merge_gate_blocked AutoDevResult, stamped by transition_task_status
    # alongside disposition/pr_url/completed_at on every terminal transition
    # and cleared on requeue/cancel. Distinct from last_blocked_result above:
    # that field is a diagnostic-only dump for the malformed/unrecognized-
    # sentinel catch-all; this field carries the specific reason string for
    # the routine, well-formed blocked park so it can surface verbatim in
    # `cw dev-queue tasks` and the attention-event formatter. None when the
    # task was never blocked, or blocked with no blocker reason (e.g.
    # scope_exceeded/forbidden_area, which carry no blocker field at all).
    blocked_reason: str | None = None
    # GitHub #1198 — operator escape hatch for the cross-repo dispatch guard.
    # When True, the address_review / auto_fix_ci recipes log a WARNING and
    # dispatch anyway even though the row's client resolves to a different repo
    # than its pr_url points at (the guard would otherwise skip + emit
    # PR_ACTION_FAILED). A plain always-on bool, distinct from the Optional
    # tiered-policy overrides above — an explicit, logged, incident-response flag.
    cross_repo_override: bool = False
    # GitHub #1361 — furthest pipeline stage this ticket has ever reached,
    # across all attempts/regressions. Distinct from `stage` (the *current*
    # pointer, which can move backward on self-heal/requeue): this field is
    # monotonic non-decreasing in pipeline order. Seeded from the task's
    # current stage on migration (v21). Raised via max()-by-pipeline-order
    # (never lowered) on every forward stage move in `_advance_task_pointer`
    # and `_apply_requeue_stage`'s forward/same-stage tail; deliberately left
    # untouched by `_stage_regress` and backward requeues. Lets a consumer
    # (queue_peek's attempt-count STOP gate) distinguish a ticket that is
    # thrashing (never got past IMPL) from one that is legitimately grinding
    # through repeated review/finalize cycles.
    stage_high_water: Stage | None = None

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
