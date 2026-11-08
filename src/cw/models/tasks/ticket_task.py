"""``TicketTask``: one ticket queued for dispatch to a Claude session.

Depends on ``cw.models.enums`` and ``cw.models.events`` and on the
``schema``, ``keys``, ``validators`` and ``records`` siblings. The
cross-module model types are imported (not merely referenced) because their
annotations resolve at class-build time under
``from __future__ import annotations``. A pydantic class cannot declare
fields in another module, so the class stays whole here; field order is the
persisted ``dev_queue.json`` layout. See ``cw.models.__init__`` for the DAG.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from cw._git import FULL_COMMIT_ID_RE
from cw.models.enums import QueueItemStatus, Stage
from cw.models.events import PrState, ShippedRedEpisode
from cw.models.gate_block import GateBlockBackoff
from cw.models.operator_constraint import OperatorConstraint
from cw.models.operator_resolution import OperatorResolution
from cw.models.review_feedback import ReviewFeedback
from cw.models.tasks.records import (
    LaunchedFixWorker,
    MustFixOverride,
    PendingFixDispatch,
    UsageLimitAct,
)
from cw.models.tasks.schema import DEFAULT_LANE, DEFAULT_STAGE, ReviewApproverKind
from cw.models.tasks.validators import (
    _SAFE_TICKET_BRANCH,
    _SAFE_TICKET_ID,
    _validate_gate_recipe_keys,
    _validate_park_on_abandoned_exit_keys,
    _validate_review_recipe_keys,
)

# Runtime (not TYPE_CHECKING) because ``finding_dispositions``' annotation
# resolves at class-build time under ``from __future__ import annotations``,
# same reason the model types above are imported. Safe against an import cycle
# only because ``cw.review_finding_dispositions`` imports nothing from ``cw``
# at module scope — see that module's "Import discipline" docstring section
# before adding one.
from cw.review_finding_dispositions import FindingDisposition


class TicketTask(BaseModel):
    """A ticket queued for dispatch to a Claude session."""

    # NOT extra=forbid — persisted/runtime state, see #1200
    ticket_id: str
    client: str
    # v49 (GitHub #2948): the ticket's explicit git branch, or None for the
    # client's default <prefix>/<ticket_id>. Read only through
    # cw.models.resolve_feature_branch. Identity, not episode state: never
    # cleared by transition_task_status, cancel or requeue. Written only by PR
    # adoption (#2943).
    branch: str | None = None
    # v50 (GitHub #2943, RFC 0016): the adopted human PR's url and the head
    # SHA the row is pinned to (reviewed at). Identity, not episode state:
    # neither is ever cleared by transition_task_status, cancel or requeue.
    # ``adopted_head_sha`` set implies ``branch`` set (model validator). The
    # pin advances only through cw.dev_queue.adoption (the session-end settle,
    # the repair lease push, and the operator's ``requeue --repin-adopted-head``).
    adopted_pr_url: str | None = None
    adopted_head_sha: str | None = None
    # v50: the transient "a cw session just ended on this adopted row" marker.
    # Stamped by transition_task_status on every RUNNING exit of an adopted
    # row (a pure in-memory write); cleared only by the lock-free session-end
    # settle (cw.dispatch.adopted_head_gate), the post-repair pin advance and
    # the requeue re-pin. While set, the drift gate skips the row.
    adopted_repin_due: bool = False
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
    # The instant of the claim that last moved this row to RUNNING (v44,
    # GitHub #2591), stamped with a fresh clock at the single RUNNING entry
    # (dispatch/claim/screening.py's _screen_and_claim), next to the
    # `attempts` increment. Only meaningful while the row is RUNNING, and
    # deliberately has no clear site: the next claim overwrites it.
    # cw.reconcile.unowned_running reads it so a cw-context.json left by an
    # earlier row's session (one that started before this claim) is never
    # taken as this claim's. None on rows claimed before the field existed.
    claimed_at: datetime | None = None
    # Subset of `attempts`: claims that exited RUNNING having produced no
    # evidence of progress. This — not raw `attempts` — is what the global
    # attempt ceiling compares against (dispatch/claim.py, reconcile/
    # concierge/). Charged at the single transition_task_status seam, which
    # defaults to charging: a caller must pass unproductive=False to decline,
    # so a crash/phantom/wedge revert with no sentinel counts by construction.
    # Deliberately NOT read by the #756 per-stage validation_failed cap in
    # reconcile/_shared/_routing.py, which stays on raw `attempts`. See GitHub #1750.
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
    # v43 companion to finalize_regress_branch_head (#2421): whether MERGE_HEAD
    # was actually present in the ticket's worktree at the moment this
    # FINALIZE-origin (or manual-operator) regress fired, per a live
    # merge_in_progress() measurement -- NOT derived from blocker_reason
    # (FINALIZE_REGRESS_BLOCKER_REASONS has a single member covering both
    # merge and non-merge causes, and _apply_requeue_stage's manual regress
    # path carries no blocker at all). Stamped by _stage_regress's two call
    # sites; cleared alongside finalize_regress_branch_head by
    # _consume_finalize_regress_repeat. Read by dispatch/impl_gates.py.
    finalize_regress_merge_conflict_detected: bool = False
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
    # Tracker-neutral record that the PLAN-stage approval gate was released
    # (v35). Stamped by dev_queue/approval.py's _approve_ticket_locked whenever
    # a PLAN-stage row is approved — on the #968 same-stage re-park (plan not
    # yet quality-reviewed) as well as on the direct advance — and threaded
    # into the worker's cw-context.json queue_metadata at spawn (spawn.py).
    # auto-dev-plan.md's Checkpoint 1 reads it as operator approval evidence
    # alongside an approving tracker reply, so a Linear-tracked ticket (whose
    # tracker the GitHub-only `--post-marker` comment cannot reach) no longer
    # re-parks at plan_pending_approval on every re-dispatch. Cleared only by
    # _stage_regress into Stage.PLAN (a re-plan revokes it, #2102); otherwise
    # durable, like the GitHub marker comment it stands in for.
    plan_approved_at: datetime | None = None
    # The fingerprint of the draft plan_approved_at above was given for (v36,
    # #2102) — read from the approving session's sentinel
    # (AutoDevResult.plan_draft_fingerprint) and threaded to the next worker via
    # spawn.py's queue_metadata, where Checkpoint 1 compares it against the
    # draft it is about to auto-skip. A mismatch means the text changed after
    # the operator approved it, so the approval does not transfer and the round
    # re-parks.
    #
    # Sibling of hook_context_conflict_session_id / finalize_regress_branch_head
    # in role: a value stamped at an existing field's own seam purely to carry
    # comparison data forward. Its seams are therefore plan_approved_at's,
    # exactly — same stamp site, same single clear site — never its own. None
    # means "no fingerprint bound": either a pre-#2102 producer emitted no
    # fingerprint, or no draft existed at approval time. Checkpoint 1 treats
    # that as absent evidence, not as a wildcard match.
    plan_approved_fingerprint: str | None = None
    # The operator's plan_scope_drift grant (v40, #2337): the sorted, deduped
    # repo-relative paths `cw dev-queue approve --scope-drift` allowed beyond
    # the plan's Files Modified. Stamped by dev_queue/scope_drift.py's
    # _approve_scope_drift_locked, only on a row parked at IMPL with
    # blocked_reason plan_scope_drift; threaded into the worker's
    # cw-context.json queue_metadata at spawn (spawn.py), where Step 2.5 gate 2
    # passes it to check_plan_scope_conformance.py as an allowlist.
    #
    # Unlike plan_approved_at above, a per-arrival marker, not a durable fact:
    # cleared unconditionally by the next spawn (dispatch/claim.py's
    # spawn-success block, beside regressed_into_stage), because the grant is
    # for exactly the IMPL session the approval requeues and must never leak
    # into a later stage entry or a later round's drift. None = no grant.
    scope_drift_approved_extra_files: list[str] | None = None
    # The branch's origin HEAD SHA when the grant above was given — its binding,
    # in the role plan_approved_fingerprint plays for plan_approved_at. Gate 2
    # honors the grant only while this SHA is an ancestor of origin/<branch>
    # (more commits on top are fine; a force-push or rewrite voids it). Same
    # stamp and clear seams as scope_drift_approved_extra_files, never its own.
    scope_drift_approved_head: str | None = None
    # The operator's codex MUST_FIX override (v42, #2205), stamped by
    # dev_queue/must_fix_override.py on a row parked BLOCKED_ON_USER with
    # blocked_reason codex_must_fix_findings. Unlike blocked_reason, it survives
    # the PENDING reset `requeue --stage finalize` performs, and is threaded
    # into cw-context.json queue_metadata at every spawn (spawn.py) for
    # FINALIZE's check_must_fix_override.py. Cleared only by _stage_regress
    # into Stage.REVIEW: a re-review supersedes the verdict it was bound to.
    must_fix_override: MustFixOverride | None = None
    # The REVIEW-stage approval record (v48, #2767), stamped through
    # dev_queue/lifecycle.py's _record_review_approval: by `approve` (or the
    # auto_approve_clean_review recipe) releasing a review gate, and (#2779,
    # "operator") by the MUST_FIX override and the review-less lane release.
    # approver_kind
    # is the provenance ("operator" for the human approve, "recipe" for the
    # gate recipe); approved_sha is the sentinel's review.reviewed_sha, None
    # when it carried none (recorded absence, never a wildcard). Threaded into
    # cw-context.json queue_metadata.review_approval at spawn; finalize Step
    # 4.0 ships a large ticket only while that SHA is an ancestor of HEAD with
    # nothing but default-branch merges after it. Cleared by every
    # _stage_regress and by a same-stage REVIEW requeue.
    review_approved_at: datetime | None = None
    review_approver_kind: ReviewApproverKind | None = None
    review_approved_sha: str | None = None
    # The operator-supplied review report tied to one repo, PR number, and exact
    # head SHA (v51). Serialized into every spawn's queue_metadata; the
    # implementation prompt treats it as untrusted evidence. Kept while the row
    # stays at IMPL (parks, resumes, same-stage requeues); cleared when it
    # leaves IMPL by any route, whoever set it (#3018, lifecycle.
    # _consume_red_recovery_evidence).
    review_feedback: ReviewFeedback | None = None
    # Red-shipped-PR episode latch (v53, #2945): one page per episode, cleared
    # by transition_task_status when the row leaves COMPLETED.
    shipped_red: ShippedRedEpisode | None = None
    # The reviewed_sha of the feedback `recover-red-pr` attached (v53, #2945).
    # NOT cleared on a transition (it must survive IMPL parks and resumes);
    # cleared with the feedback when the row leaves IMPL (lifecycle.
    # _consume_red_recovery_evidence). Only that verb sets it; nothing reads it.
    red_pr_recovery_sha: str | None = None
    # Recovery disarms auto-merge before regressing a shipped row. This stamp
    # records that finalize must re-arm it (v54, #2993), and stays set while
    # the row is parked/cancelled. The companion stamp is the one-shot latch
    # for cancellation/overdue-park attention; all clear only after verified
    # finalize re-arm or a confirmed MERGED/CLOSED PR observation.
    auto_merge_rearm_pr_url: str | None = None
    auto_merge_rearm_pr_state: PrState | None = None
    auto_merge_rearm_owed_at: datetime | None = None
    auto_merge_rearm_attention_at: datetime | None = None
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
    # PR URL for shipped tasks (disposition="shipped"), also stamped by the
    # merge_pending, automerge_not_armed and stale_dispatch_gate (#2740) parks.
    # Cleared on PENDING/CANCELLED (requeue/cancel); None otherwise.
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
    # GitHub #2717: consecutive gate-block parks, separate from
    # spawn_error_count/unproductive_attempts and NOT cleared by
    # transition_task_status. Reset by a non-gate routed outcome; its wait is
    # cleared by requeue and the Variant B AUTO release. ``until`` guards the
    # shared next_eligible_at clock. Written only by cw.dev_queue.gate_backoff.
    gate_block: GateBlockBackoff | None = None
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
    # GitHub #2307 — the Session a live-writer/inconclusive-scan codex-orphan
    # park left ACTIVE (cw.reconcile.codex_boot). session_id is cleared on
    # park, so without this nothing links the parked row back to that
    # session. Stamped by _park_running_task_blocked_on_user only for the
    # parks whose session stays open; read by cw.reconcile.codex_reparks,
    # which re-scans for the writer on reconcile ticks and closes the session
    # once it is affirmatively gone. Unlike hook_context_conflict_session_id
    # (cleared only by a successful spawn), this describes the current park
    # episode, so transition_task_status clears it on every transition.
    codex_orphan_session_id: str | None = None
    # GitHub #2307 — earliest time codex_reparks may re-scan the process
    # table for the row above. Fixed interval (a writer exits once; nothing
    # worsens to justify an escalating curve), same unconditional clear.
    codex_orphan_rescan_next_eligible_at: datetime | None = None
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
    # here wins over LaneConfig.gate_recipes and the hardcoded floor (on). None
    # (or a recipe absent from the map) defers to the lane map, then the
    # default. Recognised keys: "auto_approve_clean_review",
    # "auto_adopt_clean_plan".
    gate_recipes: dict[str, bool] | None = None
    # Ticket-level review-recipe enablement override (RFC 0010 P3, #1098).
    # Highest tier in resolve_review_recipe_enabled's 3-tier precedence: a
    # recipe present here wins over LaneConfig.review_recipes and the hardcoded
    # default-off. None (or a recipe absent from the map) defers to the lane
    # map, then the default. Recognised keys: "address_review", "auto_fix_ci",
    # "request_reviewer", "escalate_merge_block" (RFC 0010 P4, #1099),
    # "repair_changelog_conflict" (#2771), "repair_mechanical_conflict" (#2917).
    # Sibling to gate_recipes, not a reuse: review recipes react to PR attention
    # states (changes_requested / ci_failing / no_reviewer / merge_blocked), a
    # distinct action class from the approval-gate recipes above.
    review_recipes: dict[str, bool] | None = None
    # Ticket-level override for the Stop-hook abandoned-exit park (#2135,
    # schema v38). Highest tier in resolve_park_on_abandoned_exit_enabled's
    # 3-tier precedence: the key present here wins over
    # LaneConfig.park_on_abandoned_exit and the hardcoded default-off. None (or
    # the key absent from the map) defers to the lane map, then the default.
    # Recognised key: PARK_ON_ABANDONED_EXIT_KEY.
    park_on_abandoned_exit: dict[str, bool] | None = None
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
    # TicketTask has none today. Lets an operator distinguish "sentinel never
    # arrived" from "a rejected sentinel landed this FAILED."
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
    # GitHub #2401 widens this from "populated exclusively by that one
    # catch-all" to every FAILED/abandoned landing in
    # _route_blocked_result_to_task: the deterministic-parse and
    # validation_failed branches now also write it once their shared
    # _VALIDATION_FAILED_MAX_ATTEMPTS attempt cap is reached. It is still
    # never written by a re-queue (PENDING outcome) -- a re-queue rejects
    # nothing, so there is no sentinel to record here; that outcome's trace is
    # the SENTINEL_BLOCKED_RESULT_REQUEUED event instead.
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
    # v37: advisory_note (#1762) — per-tick operator advisory for a row that is
    # NOT parked.
    # `blocked_reason` is stamped only on terminal transitions, so it can never
    # carry a signal about an ordinary RUNNING row; this field is the non-
    # terminal counterpart, rendered in the same `cw dev-queue tasks` REASON
    # column when `blocked_reason` is unset (blocked_reason always wins).
    # Two writers. cw.reconcile._shared._stamp_session_id_mismatch_advisories
    # visits only RUNNING rows WITH a session_id; it overwrites the note every
    # tick and clears it the moment the condition lifts — a live
    # re-derivation, not a latch. cw.reconcile.lost_session_rows (#2715) writes
    # "?no_session" on a RUNNING row with NO session_id after paging it once —
    # a page-once latch the mismatch stamper never visits, which it clears once
    # the row is bound. Cleared unconditionally by transition_task_status
    # alongside the durable-escalation latches, so a fresh claim pages again.
    # Readers (cw dev-queue tasks, ticket_view, queue_peek) show it as an
    # advisory. None on every pre-v37 row (migrate.py backfills it).
    advisory_note: str | None = None
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
    # backend. Keys extend a cw.review_debt.fingerprint_v1 identity with a
    # digest of the verbatim summary (``"<file>::<normalized summary>::<sha256
    # of the exact summary>"``, see
    # cw.review_finding_dispositions._disposition_key); values record the
    # operator's outcome, their rationale, and when it was recorded.
    #
    # Lives on the queue row rather than in ``.cw/`` or on the tracker alone
    # because neither survives what this memory has to survive:
    # dispatch/gating/context_json.py deletes ``.cw/context.json`` on a rescued
    # respawn, and a live tracker fetch degrades to nothing on an unresolvable
    # tracker or a gh failure. The tracker marker remains the operator's INPUT
    # surface; this field is the durable record derived from it.
    #
    # No clear site, deliberately — the opposite convention to the per-arrival
    # markers above (regressed_into_stage, pending_operator_comment), which are
    # consumed once and reset. A settled adjudication is a durable fact about
    # the ticket; clearing it on requeue would re-open every finding the
    # operator has already closed, which IS the bug this field fixes.
    finding_dispositions: dict[str, FindingDisposition] = Field(default_factory=dict)
    # GitHub #2702 — the operator's `cw dev-queue resolve` answers, in round
    # order. No clear site, like finding_dispositions: durable and
    # append-only, it survives every requeue.
    operator_resolutions: list[OperatorResolution] = Field(default_factory=list)
    # GitHub #2964 — the operator's `cw review constrain` entries, in round
    # order: binding input the codex review prompt and fix loop read from the
    # row on every tracker. No clear site, like operator_resolutions: durable
    # and append-only, it survives every requeue and regress. No edit,
    # supersede or revoke path exists yet (retraction is tracked in #2968).
    operator_constraints: list[OperatorConstraint] = Field(default_factory=list)
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
    # GitHub #2590 — see LaunchedFixWorker. Stamped only on the
    # WorkerLaunchedError path of the fix dispatch, and cleared only by
    # cw.reconcile.fix_dispatch_hold once a readable roster confirms the
    # worker stopped. Deliberately NOT cleared by transition_task_status: a
    # cancel or requeue must not release a row whose worker may still run.
    fix_dispatch_launched_worker: LaunchedFixWorker | None = None
    # GitHub #2324 — the mid-turn usage-limit act's write-ahead intent; see
    # UsageLimitAct. Only ever set on a RUNNING row.
    usage_limit_act: UsageLimitAct | None = None

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

    @field_validator("park_on_abandoned_exit")
    @classmethod
    def _check_park_on_abandoned_exit(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_park_on_abandoned_exit_keys(value)

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

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        # None is "no explicit branch"; "" is rejected rather than read as None
        # so a writer cannot persist an ambiguous blank.
        if value is not None and not _SAFE_TICKET_BRANCH.match(value):
            msg = (
                f"Invalid branch {value!r}: must start with alphanumeric"
                " and contain only [a-zA-Z0-9/_.-], with no '..' sequence"
            )
            raise ValueError(msg)
        return value

    @field_validator("adopted_head_sha")
    @classmethod
    def _check_adopted_head_sha(cls, value: str | None) -> str | None:
        if value is not None and not FULL_COMMIT_ID_RE.fullmatch(value):
            msg = (
                f"Invalid adopted_head_sha {value!r}:"
                " must be a full lowercase commit SHA"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_adopted_pin_has_branch(self) -> Self:
        # RFC 0016 R3: a pinned head is meaningless without the branch it
        # pins; the drift gate and the settle read the head of ``branch``.
        if self.adopted_head_sha is not None and self.branch is None:
            msg = (
                "adopted_head_sha is set but branch is None:"
                " an adopted pin needs its branch"
            )
            raise ValueError(msg)
        return self
