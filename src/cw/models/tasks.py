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

from cw.models.enums import OCCUPIED_LANE_STATUSES, QueueItemStatus, ReapReason, Stage
from cw.models.events import PrState, WatchedPr

# Runtime (not TYPE_CHECKING) because ``finding_dispositions``' annotation
# resolves at class-build time under ``from __future__ import annotations``,
# same reason ``WatchedPr`` is imported above. Safe against an import cycle
# only because ``cw.review_finding_dispositions`` imports nothing from ``cw``
# at module scope — see that module's "Import discipline" docstring section
# before adding one.
from cw.review_finding_dispositions import FindingDisposition

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
# v23: added TicketTask.hold_finalize (GitHub #1160, RFC 0011 A3).
# v24: added TicketTask.attention_digest_buffered_at (GitHub #1162, RFC 0011
#      A6) — durable buffer-membership marker for the operator-channel
#      session.needs_attention digest coalescer (cw.cw_operator_events).
# v25: added TicketTask.salvage_no_sentinel_at (GitHub #1638) — timestamp of
#      the most recent salvage-without-sentinel park via reconcile/salvage.py's
#      LOW path (the "stopped without ever emitting a sentinel" marker).
#      Stamped by transition_task_status, not salvage.py directly (R2 seam).
#      Deliberately NOT cleared on requeue/unblock/cancel — a diagnostic-only
#      historical fact, mirroring last_blocked_result's no-clear-site
#      convention (#1266), not the escalation-latch convention: erasing it the
#      moment unblock_ticket reverts the row to PENDING would destroy the
#      exact evidence this field exists to preserve.
# v26: added TicketTask.hook_context_conflict_session_id (GitHub #1674) — the
#      session whose still-live cw-context.json made the last spawn attempt
#      raise HookContextConflictError. Stamped by the dispatch claim path,
#      read by concierge recipe 1 to refuse a requeue that cannot succeed.
# v27: added TicketTask.regressed_into_stage (GitHub #1794) — per-arrival
#      marker telling the impl-stage Pre-Stage Detector Guard that this stage
#      entry was reached via a deliberate backward move, distinct from the
#      cumulative, never-reset-on-advance regress_attempts.
# v28: added TicketTask.finalize_regress_branch_head (GitHub #1717) —
#      branch-head oracle stamped by _stage_regress only when regressing FROM
#      Stage.FINALIZE, consumed (read-and-cleared) once by dispatch/routing.py's
#      REVIEW-scoped gates to detect a finalize->impl->review round trip that
#      landed back at REVIEW with no new commit — the #1644/#1702/#1710 silent
#      repeat-park incident.
# v29: added TicketTask.pending_operator_comment (GitHub #1730) — per-arrival
#      marker telling the REVIEW stage that this entry followed a regress and
#      may carry an operator send-back to treat as a binding adjudication
#      input. Sibling of v27's regressed_into_stage, stamped at the same
#      _stage_regress seam as v28's finalize_regress_branch_head but under an
#      independent precondition, and cleared only at a REVIEW-stage spawn.
# v30: added TicketTask.stale_gate_detected_at/blocked_on_pr (GitHub #1713) —
#      detection latch + cross-reference field for
#      cw.reconcile.tasks.release_stale_gated_tasks, which re-validates a
#      BLOCKED_ON_USER row's gate condition (own PR merged, or the PR it is
#      blocked behind merged) against fresh pr.merged events/hydrated
#      pr_state, since dev-queue rows otherwise never observe that clearing.
#      stale_gate_detected_at follows the escalation_parked_at/
#      gate_recipe_failed_at unconditional-clear-on-transition convention;
#      blocked_on_pr is a bare int PR number (no owner/repo qualifier — a
#      dev-queue client is bound to exactly one repo).
# v31: added TicketTask.finding_dispositions (GitHub #1838) — the durable
#      cross-round adjudication ledger, keyed by a stringified
#      cw.review_debt.fingerprint_v1. Stamped by
#      cw.codex_background._sync_finding_dispositions_to_running_task once a
#      review pass has merged the ticket thread's REVIEW-FINDING-DISPOSITIONS
#      marker into it. Deliberately has NO clear site: it is durable memory,
#      not a per-arrival marker like v27/v29 — forgetting a settled
#      adjudication on requeue is the exact failure #1838 exists to remove.
# v32: added TicketTask.unproductive_attempts (GitHub #1750) — a second,
#      narrower attempt counter beside the raw `attempts` claim counter. Only
#      claims that exit RUNNING with no evidence of progress (no commits, no
#      review findings, no consumed operator resolution) are charged; the
#      global dispatch attempt ceiling reads this counter instead of
#      `attempts`, so a ticket making real forward progress can no longer be
#      parked at `attempt_cap_blocked` for being busy (#1727) while the
#      crashloop the ceiling exists to catch (#1653) is still bounded.
#      Incremented at exactly one seam (transition_task_status), mirroring
#      regress_attempts's v6 single-seam precedent.
# v33: added TicketTask.ever_spawned (GitHub #1631) — durable "a session was
#      genuinely spawned for this row at least once" marker. Exists because
#      the two attempt counters move asymmetrically on the UsageLimitError
#      revert path (attempts increments, spawn_error_count deliberately does
#      not — #868 fleet-wide backoff), which left a usage-limit-only history
#      structurally identical on the task record to a task that ran, shipped
#      and timed out inside its first stage, and therefore silently
#      auto-completable by reconcile's timed-out-merged backstop. Stamped
#      True at exactly one seam (dispatch/claim.py's spawn-success block),
#      seeded False at exactly one seam (dev_queue_add, the only place with
#      positive proof a row has never spawned), and — like v31's
#      finding_dispositions — deliberately has NO clear site: it is a
#      permanent historical fact, not a per-arrival marker. Model default and
#      migration fill are both True (fail-open): state we cannot reconstruct
#      must not retroactively refuse a legitimate completion.
# v34: added TicketTask.pending_fix_dispatch/fix_dispatch_session_id
#      (GitHub #2017) — the durable, non-worktree handoff surface the review
#      fix loop's asynchronous dispatch runs on. See PendingFixDispatch below.
DEV_QUEUE_SCHEMA_VERSION = 34
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


class PendingFixDispatch(BaseModel):
    """Durable action-list handoff for the review fix loop (GitHub #2017 R21).

    Written by the REVIEW session under ``dev_queue_lock()`` before it exits;
    consumed by ``cw.reconcile.fix_dispatch`` on a later reconcile tick, from a
    process that is never resident in the ticket's worktree. That separation is
    the whole point: ``cw.spawn._write_hook_context``'s DAEMON guard refuses any
    spawn into a worktree whose ``cw-context.json`` references a still-live
    session, so a review session can never dispatch a fix session into its own
    worktree. By the time this record is consumed the writing session is
    terminal by construction, and the guard is satisfied without an exemption.

    Carries the prompt TEXT, not a path: the prompt *is* the action list, and a
    worktree-local file is not a durable surface (R21.4) — it dies with the
    worktree. Lives in ``dev_queue.json``, which outlives both.
    """

    # NOT extra=forbid — persisted/runtime state, see #1200
    prompt: str
    label: str
    cycle: int
    requested_by_session_id: str
    requested_at: datetime


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
    # Subset of `attempts`: claims that exited RUNNING having produced no
    # evidence of progress. This — not raw `attempts` — is what the global
    # attempt ceiling compares against (dispatch/claim.py, reconcile/
    # concierge.py). Charged at the single transition_task_status seam, which
    # defaults to charging: a caller must pass unproductive=False to decline,
    # so a crash/phantom/wedge revert with no sentinel counts by construction.
    # Deliberately NOT read by the #756 per-stage validation_failed cap in
    # reconcile/_shared.py, which stays on raw `attempts`. See GitHub #1750.
    unproductive_attempts: int = 0
    # Durable proof that a session was genuinely spawned for this row at least
    # once — the state reconcile/tasks.py's _is_never_claimed needs and cannot
    # derive from the counters above. `attempts` increments at claim time, but
    # the UsageLimitError revert path calls _revert_claimed_task_to_pending
    # with stamp_backoff=False, so spawn_error_count does NOT move in lockstep
    # (deliberate: #868's fleet-wide backoff must not charge a usage limit as
    # a spawn error). A row whose every attempt died that way therefore looks
    # exactly like one that ran, shipped and timed out inside its first stage.
    #
    # Written True at exactly one seam: dispatch/claim.py's spawn-success
    # block, alongside the session_id stamp. Seeded False at exactly one seam:
    # dev_queue_add (cli/dev_queue/crud.py), the only construction site with
    # positive proof the row has never spawned.
    #
    # The model default is True, NOT False, and that asymmetry is load-bearing
    # rather than stylistic: it matches the migration fill (fail-open — a
    # legacy row carries no record of its spawn history, and refusing its
    # completion retroactively would be worse than the bug), and it keeps
    # every TicketTask built without the field — test fixtures, and any future
    # construction site that has no such proof — out of the refusal branch.
    #
    # Deliberately has NO clear site: it survives every requeue, regress,
    # revert and retry. This is the v31 finding_dispositions convention (a
    # durable historical fact), not the v27/v29 per-arrival-marker convention;
    # forgetting that a spawn once succeeded is exactly the amnesia #1631
    # exists to remove. See GitHub #1631.
    ever_spawned: bool = True
    # Number of times the task has been auto-regressed from FINALIZE back to
    # IMPL for self-heal (e.g. diff-cover gate failures). Bounded by
    # FINALIZE_REGRESS_CAP in auto_dev_result.py. See GitHub #770.
    regress_attempts: int = 0
    # Per-arrival marker: set to the target stage by _stage_regress whenever a
    # regress (operator `--regress`, or FINALIZE self-heal) lands the task
    # there; cleared by the next real dispatch spawn for this task
    # (dispatch/claim.py), or by an intervening forward requeue bypass
    # (requeue.py). Distinct from the cumulative regress_attempts above: this
    # field answers "was THIS stage entry reached via a backward move," not
    # "how many regresses has this ticket ever had." Reusing regress_attempts
    # for this purpose produces a false positive once any later, unrelated
    # forward advance crosses the same stage (GitHub #1794); it also cannot be
    # reset on ordinary advance without defeating the FINALIZE self-heal cap
    # (GitHub #770, FINALIZE_REGRESS_CAP in auto_dev_result/schema.py).
    #
    # Accepted limitation (GitHub #1801): this field does NOT survive a
    # spawn that dies with no sentinel ever emitted. claim.py's spawn-time
    # clear (below/#1794) runs the moment the marker is written into the
    # worker's queue_metadata -- well before any reap could run -- so a
    # bare `--regress` whose first post-regress session dies silently loses
    # the signal for good. #1801 evaluated changing the clear to a
    # sentinel-gated one and rejected it: it would fragment the shared
    # `_stage_regress` seam this field co-stamps with
    # `pending_operator_comment`/`finalize_regress_branch_head` (each
    # already clears on its own independent rule), and it reintroduces the
    # same false-negative shape at Orientation's early `blocked` exit
    # (before the Pre-Stage Detector Guard ever runs). The comment-staleness
    # check remains the backstop for the common case; this is a rare,
    # already-partially-mitigated compound trigger, not silently unhandled.
    regressed_into_stage: Stage | None = None
    # Branch-head oracle for the #1717 FINALIZE-regress repeat detector: set
    # by _stage_regress to the pre-regress stage_base_ref, but ONLY when
    # regressing FROM Stage.FINALIZE (the #770 self-heal round trip this field
    # exists to close the loop on). Consumed (read-and-cleared) exactly once,
    # by dispatch/routing.py's REVIEW-scoped gates on the round trip's first
    # REVIEW re-entry — compared against the freshly-restamped stage_base_ref
    # to detect "no commit landed anywhere in the round trip," which would
    # otherwise silently re-fire an identical park with no operator-visible
    # signal that this is a repeat (GitHub #1644, #1702, #1710). Unrelated to
    # regressed_into_stage above: distinct field, distinct owner ticket, both
    # stamped at the same _stage_regress seam under independent preconditions
    # (see the shared-seam comment there).
    finalize_regress_branch_head: str | None = None
    # Per-arrival marker: raised to True by _stage_regress alongside
    # regressed_into_stage above (same stamp point, same unconditional style),
    # signalling that this re-entry followed a backward move and may therefore
    # be carrying an operator send-back comment the reviewer must treat as a
    # BINDING adjudication input rather than background context (GitHub #1730).
    #
    # Consumption timing deliberately DIVERGES from regressed_into_stage, which
    # dispatch/claim.py clears at the very next spawn regardless of stage: this
    # marker is cleared only at a spawn where task.stage == Stage.REVIEW. Rule
    # 5a's FINALIZE self-heal (dispatch/routing.py) regresses to Stage.IMPL, not
    # REVIEW, so an unconditional clear would consume-and-drop the marker at the
    # IMPL spawn -- long before the task advances IMPL -> REVIEW, which is the
    # only stage where it means anything.
    #
    # A plain bool suffices: unlike finalize_regress_branch_head directly above
    # -- its sibling at the same _stage_regress seam, whose whole purpose is to
    # carry a value forward for later comparison -- this marker carries no
    # comparison data of its own. The comment *content* is delivered separately
    # and unconditionally by the review stage's own live-fetch (both backends);
    # this field only says "treat what you are about to read as elevated".
    pending_operator_comment: bool = False
    # DEPRECATED — inert since the process-kill-timeout removal. Formerly the
    # per-ticket wall-clock budget override (#265); nothing consults it now.
    # Kept only so persisted dev-queue rows that carry the field keep loading.
    headless_timeout_override: int | None = None
    # DEPRECATED — inert since the process-kill-timeout removal. Formerly the
    # per-ticket idle-watchdog budget override (#326); nothing consults it now.
    # Kept only so persisted dev-queue rows that carry the field keep loading.
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
    # The session whose still-non-terminal cw-context.json made the last spawn
    # attempt raise HookContextConflictError (GitHub #1674). A third companion
    # field in the same convention as the two pairs above — stamped by the
    # dispatch claim path's narrow HookContextConflictError handler, cleared on
    # any successful spawn. Concierge recipe 1 reads it to refuse requeuing a
    # row whose currently-resolved session IS this one and is still
    # non-terminal: that worktree cannot be reused until the session is closed,
    # so every requeue burns an attempt for nothing.
    hook_context_conflict_session_id: str | None = None
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
    # Ticket-level proactive finalize-hold override (RFC 0011 A3). Takes
    # precedence over LaneConfig.finalize_gate and
    # OrchestratorConfig.default_finalize_gate in resolve_hold_finalize's
    # 3-tier resolution. None means "no ticket-level override -- fall through
    # to lane/global". Set via ``cw dev-queue add --hold-finalize``.
    #
    # Sibling to `signoff` above, not a duplicate of it: signoff parks the row
    # AWAITING_OPERATOR_SIGNOFF (an authorization state a second `approve`
    # clears), whereas this force hold parks it BLOCKED_ON_USER with the
    # `finalize_gate_held` hold disposition and wins outright when both are
    # armed -- a proactive stop-before-unattended-finalize, not a second
    # signature slot. See GitHub #1160.
    hold_finalize: Literal["manual"] | None = None
    # GitHub #1162 (RFC 0011 A6) — durable buffer-membership marker for the
    # operator-channel session.needs_attention digest coalescer
    # (cw.cw_operator_events). Stamped (only if unset) the first time this
    # task's held disposition (HOLD_DISPOSITIONS) admits a
    # session.needs_attention event that gets buffered instead of forwarded
    # immediately; None means "not currently buffered." Cleared to None both
    # by a real digest flush (poll_and_forward_operator_channel) and
    # unconditionally by transition_task_status on every status transition
    # (alongside escalation_parked_at/gate_recipe_failed_at above) — the
    # latter is what satisfies R9's "re-derive live state, never replay a
    # resolved episode" requirement for free, via the same mutation seam.
    attention_digest_buffered_at: datetime | None = None
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
    # GitHub #1406 narrows that further: the catch-all now exits early, without
    # writing this field, when the owning session's transcript is still live
    # (the sentinel is re-queued PENDING, not rejected). So a set value means
    # "rejected AND the worker was demonstrably done on that landing" -- not
    # merely "rejected." Not a lifetime guarantee: a --from-failed requeue
    # (dev_queue/requeue.py's requeue_ticket, via the shared
    # _reset_for_same_stage_requeue in dev_queue/lifecycle.py) doesn't clear
    # this field, so a stale value from an earlier FAILED landing can persist
    # on a task later revived to PENDING/RUNNING -- read it relative to the
    # task's *current* status.
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
    salvage_no_sentinel_at: datetime | None = None
    # GitHub #1838 — cross-round adjudication memory for the codex review
    # backend. Keys are stringified cw.review_debt.fingerprint_v1 identities
    # (``"<file>::<normalized summary>"``, see
    # cw.review_finding_dispositions._disposition_key); values record the
    # operator's outcome, their rationale, and when it was recorded.
    #
    # Lives on the queue row rather than in ``.cw/`` or on the tracker alone
    # because neither survives what this memory has to survive:
    # dispatch/gating.py deletes ``.cw/context.json`` on a rescued respawn, and
    # a live tracker fetch degrades to nothing on an unresolvable tracker or a
    # gh failure. The tracker marker remains the operator's INPUT surface; this
    # field is the durable record derived from it.
    #
    # No clear site, deliberately — the opposite convention to the per-arrival
    # markers above (regressed_into_stage, pending_operator_comment), which are
    # consumed once and reset. A settled adjudication is a durable fact about
    # the ticket; clearing it on requeue would re-open every finding the
    # operator has already closed, which IS the bug this field fixes.
    finding_dispositions: dict[str, FindingDisposition] = Field(default_factory=dict)
    # GitHub #1713 — durable detection latch for
    # cw.reconcile.tasks.release_stale_gated_tasks: stamped when a
    # BLOCKED_ON_USER row's gate condition is observed to have cleared (its
    # own PR merged, or the PR it is blocked behind merged) but ReapPolicy is
    # SIGNAL_ONLY, so the row is not yet auto-released. Same
    # unconditional-clear-on-every-transition convention as
    # escalation_parked_at/gate_recipe_failed_at above (transition_task_status
    # clears it on any real status transition) -- a fresh parked episode
    # always starts with a clean latch.
    stale_gate_detected_at: datetime | None = None
    # GitHub #1713 — Variant B's blocking PR number: bare int, no owner/repo
    # qualifier (a dev-queue client is bound to exactly one repo; see the
    # ticket's Self-Verified Premises). Stamped by dispatch/routing.py's Rule
    # 5 when a `merge_gate_blocked`/`prior_pipeline_pr_open` park or a
    # `stale_dispatch`/`pr_already_open` park (GitHub #1902 fast-follow to
    # #1862) has a blocker.details naming the blocking PR. Cross-referenced
    # by release_stale_gated_tasks against another task's hydrated pr_state
    # within the same client to detect when that blocking PR has merged --
    # or, for the stale_dispatch producer (whose blocking PR is this
    # ticket's own and lives on no task row), against the client-tagged
    # WatchedPr that cw.reconcile.stale_dispatch_watch registers for it
    # (GitHub #1927).
    blocked_on_pr: int | None = None
    # GitHub #2017 — the review fix loop's asynchronous handoff pair. The
    # REVIEW session records `pending_fix_dispatch` and exits; a later
    # reconcile tick (cw.reconcile.fix_dispatch) dispatches the fix session
    # from outside any worktree, clears the record, and stamps
    # `fix_dispatch_session_id` so the completion watcher can revert the row to
    # PENDING once that session goes terminal.
    #
    # Deliberately NOT accompanied by a status transition at record time: the
    # row stays RUNNING for the whole handoff, which is what keeps it invisible
    # to dispatch/claim.py's PENDING-only reclaim check and so prevents a second
    # REVIEW session being dispatched before the fix agent has even spawned.
    #
    # Per-arrival markers in the v27/v29 convention, not durable history: each
    # is consumed and cleared by exactly one seam in fix_dispatch.py.
    pending_fix_dispatch: PendingFixDispatch | None = None
    fix_dispatch_session_id: str | None = None

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


def occupies_lane_slot(task: TicketTask) -> bool:
    """True iff *task* counts toward its lane's ``OCCUPIED_LANE_STATUSES`` cap.

    GitHub #2100. Mirrors ``OCCUPIED_LANE_STATUSES`` membership with exactly
    one carve-out: a BLOCKED_ON_USER row parked with
    ``disposition == ReapReason.TERMINAL_SIBLING`` (stamped by
    ``cw.reconcile.tasks.park_terminal_sibling_tasks``) is a duplicate row a
    lock-contention race minted for a ticket whose real row already reached a
    terminal status — not live or operator-parked work. Counting it toward
    lane occupancy holds that lane slot forever behind a ticket that already
    shipped, and ``cw doctor --reap`` cannot free it (there is nothing to
    revert it *to* — see the dedicated wedge class in ``cw.doctor.wedge``).

    Every dispatch consumer that gates on ``OCCUPIED_LANE_STATUSES`` for lane
    capacity (``dispatch.tick``/``dispatch.claim``/``board``) must call this
    instead of testing membership directly, so a terminal_sibling row is
    excluded everywhere occupancy is computed, not just where it happens to
    be visible first.
    """
    if task.status not in OCCUPIED_LANE_STATUSES:
        return False
    return not (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == ReapReason.TERMINAL_SIBLING.value
    )


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
