"""Pydantic models and schema vocabulary for the ``<<<AUTO_DEV_RESULT`` sentinel.

The headless ``/auto-dev`` skill emits a sentinel-delimited JSON block as the
final lines of stdout summarizing the pipeline outcome. This module owns the
*schema half* of that contract: the :class:`AutoDevResult` model and its nested
models, plus the status/stage/scope vocabulary and cross-field invariants they
enforce. The *parsing half* (stdout extraction, decode, producer-drift
coercion, :func:`parse_stdout`) lives in :mod:`cw.auto_dev_result.parse`.

Spec: ``docs/headless-contract.md`` (§3 framing, §4 enum, §5 health, §6
failure modes). Package split: issue #1321.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cw.models import QueueItemStatus

_log = logging.getLogger("cw.auto_dev_result")


# Accepted sentinel schema versions. Single source of truth: parse.py derives
# SUPPORTED_SCHEMA_VERSIONS (its pre-Pydantic gate) from this Literal via
# get_args, so a version bump edits exactly one place (#1535 drift class).
SchemaVersion = Literal[1, 2, 3, 4, 5, 6, 7]

Status = Literal[
    "shipped",
    # RFC 0005 B2 intermediate stage-success status (#699). PR-less: IMPL
    # pushes a branch but does not create a PR (FINALIZE does). Accepted under
    # all supported schema versions (same rollout exception as _V4_STATUSES)
    # until the auto-dev-impl producer skill bumps its emitted schema_version.
    "stage_complete",
    "plan_pending_approval",
    "review_pending_approval",
    "merge_gate_blocked",
    # PR created but awaiting CI / merge gate (#899). Non-null pr required;
    # parse boundary coerces status=blocked+non-null pr to this status.
    "merge_pending",
    "scope_exceeded",
    "forbidden_area",
    "blocked",
    "no_op",
    "ambiguities_pending_resolution",
    "premises_pending_verification",
    # #1870: the branch has zero commits ahead of origin/<default_branch> at
    # IMPL/REVIEW exit, or as measured by the dispatch-level empty-diff gate --
    # never a clean pass. Post-branch (unlike scope_exceeded/forbidden_area), so
    # it carries a non-null branch and may carry a blocker. Accepted under all
    # supported schema versions (same rollout exception as _V4_STATUSES) until
    # the producer skills bump their emitted schema_version to 6.
    "empty_diff_blocked",
    # #1862: this ticket already has an open, unmerged PR from an earlier
    # dispatch, so the run refuses rather than re-implementing on top of work
    # already in review. Distinct from no_op (nothing is complete -- the PR is
    # unmerged) and from blocked (nothing is broken -- the PR is healthy, just
    # not this session's to duplicate). May be reported pre-branch (the Stage 0
    # intake self-check) or post-branch (discovered mid-IMPL on a resume), so
    # it is NOT a _PRE_BRANCH_STATUSES member and may carry a blocker naming
    # the discovered PR. Accepted under all supported schema versions (same
    # rollout exception as _V4_STATUSES) until the producer skills bump their
    # emitted schema_version to 7.
    "stale_dispatch",
]
# Statuses introduced after v1. Emitting one under schema_version=1 is a
# producer bug — it would silently degrade for downstream tools that key off
# the version field.
_V2_STATUSES: frozenset[str] = frozenset({"no_op"})
# Lowest schema_version that may legally carry a v2-introduced status.
_MIN_V2_SCHEMA_VERSION = 2
# Statuses introduced in v4 (issue #191). Per rollout exception (issue #316),
# accepted under all supported schema versions (v2, v3, v4) until the producer
# skill bumps its emitted schema_version to v4.
_V4_STATUSES: frozenset[str] = frozenset(
    {"ambiguities_pending_resolution", "premises_pending_verification"}
)
# Named status sets for the B2 stage-advance decision table (RFC 0005 B2).
# Placement: next to PAUSED_FOR_USER_INPUT_STATUSES per R9.
SCOPE_GATED_APPROVAL_STATUSES: frozenset[str] = frozenset(
    {"plan_pending_approval", "review_pending_approval"}
)
# Public alias for consumers that need to check whether a status indicates the
# session is paused waiting for human input (issue #129). Includes the v4
# ambiguity/premises statuses plus the approval-pending states (#633).
# DRY: SCOPE_GATED_APPROVAL_STATUSES is composed in here (RFC 0005 B2).
PAUSED_FOR_USER_INPUT_STATUSES: frozenset[str] = (
    _V4_STATUSES | SCOPE_GATED_APPROVAL_STATUSES
)
STAGE_SUCCESS_STATUSES: frozenset[str] = frozenset({"shipped", "stage_complete"})
STAGE_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "blocked",
        "merge_gate_blocked",
        "scope_exceeded",
        "forbidden_area",
        "empty_diff_blocked",
        "stale_dispatch",
    }
)
# blocker.reason (open enum, §4.2) paired with the empty_diff_blocked status
# above. Named rather than inlined because two producers write it -- the
# codex-review synthesis path and the auto-dev-review producer skill -- and a
# typo in either would be invisible to the closed-enum status check. See #1870.
EMPTY_DIFF_BLOCKER_REASON: Literal["empty_diff_no_commits"] = "empty_diff_no_commits"
# blocker.reason (open enum, §4.2) paired with the stale_dispatch status above.
# Named for the same reason EMPTY_DIFF_BLOCKER_REASON is: two producers write
# it (the auto-dev-intake Stage 0 self-check and the auto-dev-plan Stage 1
# resume-path check), and a typo in either would be invisible to the
# closed-enum status check. Deliberately distinct from
# cw.dev_queue.lifecycle._PRE_DISPATCH_STALE_PR_REASON, which names the
# *code-side* gate's park -- that one is never emitted by an agent. See #1862.
STALE_DISPATCH_BLOCKER_REASON: Literal["pr_already_open"] = "pr_already_open"
# Blocker reasons at Stage.FINALIZE eligible for automatic regress to IMPL.
# "agent_block" covers prep-pr gate failures (diff-cover, etc.) that a fresh
# impl session can fix by adding missing tests. Reasons absent here (e.g.
# "no_result_emitted") stay BLOCKED_ON_USER. Open enum per §4.2 — add reasons
# as the producer skill evolves. See GitHub #770.
FINALIZE_REGRESS_BLOCKER_REASONS: frozenset[str] = frozenset({"agent_block"})
# Blocker reasons that mean "we can't reach the operator/a dependency right
# now", not "this leg is broken" (RFC 0011 A1). Distinct axis from
# FINALIZE_REGRESS_BLOCKER_REASONS above -- self-heals nothing, just tags the
# park so the attention layer and (later, A4) auto-resume can tell it apart
# from a genuine `blocked`. push_auth_failed (#1049) is retro-classified as
# the first instance.
OPERATOR_UNAVAILABLE_BLOCKER_REASONS: frozenset[str] = frozenset(
    {"push_auth_failed", "operator_unavailable"}
)
# Max automatic FINALIZE→IMPL regressions per ticket; prevents ping-pong.
FINALIZE_REGRESS_CAP: int = 2
SCOPE_TIER_SMALL: Literal["small"] = "small"
SCOPE_TIER_LARGE: Literal["large"] = "large"
# Unresolved-provenance sentinel for PlanSource, e.g. when no stage has yet
# classified how a ticket's plan originated.
PLAN_SOURCE_NONE: Literal["none"] = "none"

# AutoDevResult statuses that represent terminal outcomes the dev-queue should
# never auto-retry. A phantom or stalled session that emitted one of these
# before crashing must be salvaged (dispositioned by the sentinel) rather than
# mislabeled crashed/timed-out and re-dispatched.
#
# This is the single source of truth; both reconcile.py and cli.py import it
# so the two cannot drift apart. See GitHub issues #372 and #431.
SALVAGE_TERMINAL_STATUSES: frozenset[str] = (
    frozenset(
        {
            "shipped",
            "no_op",
            "plan_pending_approval",
            "review_pending_approval",
            "merge_gate_blocked",
            # PR created, awaiting CI/merge — do not re-dispatch (#899).
            "merge_pending",
            "scope_exceeded",
            "forbidden_area",
            # #1870. Explicit member: this set is hand-maintained and does NOT
            # derive from STAGE_FAILURE_STATUSES, so a crashed worker whose last
            # sentinel reported an empty diff would otherwise be mislabeled
            # crashed and re-dispatched onto the same empty branch.
            "empty_diff_blocked",
            # #1862. Explicit member for the same reason: a crashed worker
            # whose last sentinel reported an already-open PR would otherwise
            # be re-dispatched onto the exact ticket the sentinel just refused.
            "stale_dispatch",
        }
    )
    | PAUSED_FOR_USER_INPUT_STATUSES
)

# Stage-advance success statuses that are NOT terminal salvage targets — i.e.
# they must advance the pipeline to the next stage rather than be dispositioned
# as a terminal outcome. Today this is exactly {stage_complete}: the PR-less
# intermediate stage-success status (#699). "shipped" is excluded because it is
# in SALVAGE_TERMINAL_STATUSES (terminal-salvage already handles it). Used by the
# reconcile phantom path to route an exited worker's emitted advance sentinel
# through apply_staged_decision instead of reverting it as a crash (#716).
INTERMEDIATE_ADVANCE_STATUSES: frozenset[str] = (
    STAGE_SUCCESS_STATUSES - SALVAGE_TERMINAL_STATUSES
)

# Salvage-routing hold set (#1566): every status whose live dispatch
# Rule 1/2/5/3b routes to BLOCKED_ON_USER rather than completing the task.
# SCOPE_GATED_APPROVAL_STATUSES is not its own term here -- it is already a
# subset of PAUSED_FOR_USER_INPUT_STATUSES (see test_paused_is_superset_of_
# scope_gated). Composed from the SAME frozensets dispatch/routing.py's Rule
# 1/2/5 membership tests read, and the "merge_pending" literal Rule 3b
# matches, so a status added to one side cannot silently drift the other.
SALVAGE_HOLD_STATUSES: frozenset[str] = (
    STAGE_FAILURE_STATUSES
    | PAUSED_FOR_USER_INPUT_STATUSES
    | frozenset({"merge_pending"})
)


def queue_status_for_terminal_sentinel(status: Status) -> QueueItemStatus:
    """Classify a terminal sentinel status as a hold or a completion.

    Single source of truth for "does this status need a human before the
    ticket can move again," consumed by the reconcile salvage path
    (``cw.reconcile._shared._queue_status_for_salvaged``) so a worker that
    dies mid-sentinel is dispositioned the same way a live observer would
    have routed it (#1566). Live dispatch's Rule 1/2/5/3b
    (``cw.dispatch.routing._route_staged_decision``) does not call this
    function -- it has its own independent branch that reads the same
    underlying frozensets. The two are kept in sync by
    ``test_salvage_dispatch_hold_membership_is_single_source_of_truth``, not
    by a shared call site.

    Deliberately narrower than dispatch's routing table -- it answers only
    "is this a hold," not how to get there. It does NOT reproduce Rule 3 /
    ``_route_stage_success``'s stage-advance semantics or its
    ``_park_finalize_hold`` / ``_park_signoff_gate`` branches (salvage has no
    live task to advance -- the worker is dead), nor Rule 5a's FINALIZE-regress
    branch (salvage never regresses). Disposition computation
    (``_hold_aware_disposition``) and event emission stay dispatch/salvage-
    caller concerns.
    """
    if status in SALVAGE_HOLD_STATUSES:
        return QueueItemStatus.BLOCKED_ON_USER
    return QueueItemStatus.COMPLETED


# NOTE: stage1_pre_flight (StageReached) and "none" (PlanSource) are NOT
# gated by schema_version. Spec §8 says enum additions require a version
# bump (v3), and v3 IS the official home for these values, BUT the producer
# skill emits them under v2 today (see #103). One-time rollout exception:
# accept under v2 AND v3 until the skill bumps. When skill emits v3, this
# exception can be removed and a `_V3_STAGES`/`_V3_PLAN_SOURCES` gate added.
#
# Also accepted ungated: "github_issue_existing" (PlanSource). The producer
# emits this for GitHub-sourced runs (the post-Linear analog of
# "linear_existing"); the parser previously rejected every such run as
# validation_failed (see #190). Treated identically to "linear_existing" —
# pure producer-side relabeling, no consumer behavior change. Accepted under
# v2 and v3 (the producer emits at v2 today per captured payloads).
StageReached = Literal[
    "stage1_pre_flight",
    "stage1_plan",
    "stage2_impl",
    "stage3_review",
    "stage4a_merge_gate",
    "stage4b_pr_create",
    "stage5_post_create",
]
# Short-form stage codes emitted by the auto-dev producer's resume-detection
# substates (e.g. ``s5_ci_pending`` instead of ``stage5_post_create``).
# ``AutoDevResult._normalize_stage_reached`` maps these to their nearest
# full-form canonical equivalent before Pydantic validates the Literal.
# Unknown values pass through unchanged and fail the Literal check loudly.
# See issue #292 for the root-cause analysis.
_STAGE_REACHED_ALIASES: dict[str, str] = {
    "pre_flight": "stage1_pre_flight",
    "s1_drafting": "stage1_plan",
    "s1_pending_ambiguity_resolution": "stage1_plan",
    "s1_pending_human_approval": "stage1_plan",
    "s1_plan_approved": "stage1_plan",
    "s2_implementing": "stage2_impl",
    "s3_review_pending": "stage3_review",
    "s3_fix_loop": "stage3_review",
    "s4_pr_open": "stage5_post_create",
    "s5_ci_pending": "stage5_post_create",
    "s5_ci_passed": "stage5_post_create",
    "s5_ci_failed": "stage5_post_create",
    "merged": "stage5_post_create",
}
# Canonical StageReached values (mirrors the Literal above) — used to short-
# circuit normalization so a valid value is never re-coerced.
_STAGE_REACHED_CANONICAL: frozenset[str] = frozenset(
    {
        "stage1_pre_flight",
        "stage1_plan",
        "stage2_impl",
        "stage3_review",
        "stage4a_merge_gate",
        "stage4b_pr_create",
        "stage5_post_create",
    }
)
# Tolerant fallback for a near-miss the producer emits WITHIN a known stage
# number (e.g. ``stage4_pr_creation`` instead of ``stage4b_pr_create``).
# stage_reached is informational (routing keys on ``status``), so a stray label
# must not fail the whole sentinel and discard completed work (#748). A value
# with a ``stage<1-5>`` prefix that is neither canonical nor a known alias is
# coerced to that stage's canonical value with a WARNING. Values with no
# ``stage<1-5>`` prefix (genuine garbage) still fall through and reject, to keep
# catching malformed payloads. The stage4 prefix maps to ``stage4b_pr_create``
# (the PR-creation substage) since that is where the observed drift occurs.
_STAGE_NUMBER_FALLBACK: dict[str, str] = {
    "stage1": "stage1_plan",
    "stage2": "stage2_impl",
    "stage3": "stage3_review",
    "stage4": "stage4b_pr_create",
    "stage5": "stage5_post_create",
}
ScopeTier = Literal["small", "large"]
PlanSource = Literal[
    "linear_existing",
    "github_issue_existing",
    "generated",
    "free_text",
    "none",
]


class Scope(BaseModel):
    tier: ScopeTier | None = None
    files: int
    lines_estimate: int
    lines_actual: int | None = None
    forbidden_touched: bool


class PrInfo(BaseModel):
    number: int
    url: str
    auto_merge: bool
    base: str


class PrCreated(BaseModel):
    """Phase D — pre-merge PR snapshot captured at PR-creation time (issue #174).

    Distinct from :class:`PrInfo` (the ``pr`` field, representing the final
    shipped state). ``PrCreated`` is emitted *before* auto-merge is triggered so
    the orchestrator can attach CI watchers or make merge decisions based on the
    CI state at the moment the PR was opened, not after the merge completes.

    ``ci_status_at_creation`` is a free-form string (open-ish enum). Observed
    producer values: ``"pending"``, ``"passing"``, ``"failing"``. Consumers
    MUST treat unknown values as opaque strings and surface verbatim.
    """

    number: int
    url: str
    ci_status_at_creation: str
    auto_merge_enabled: bool


class Review(BaseModel):
    must_fix_initial: int
    should_fix: int
    fix_cycles_used: int
    deferred: int = 0
    # v5 (#1237): count of reviewer agents that ran, reconciled against the
    # executor-neutral review-verdict's `agents_run` list
    # (`len(verdict.agents_run)`, including failed entries). Advisory optional
    # field; defaults to 0 on payloads from producers that predate v5.
    agents_run: int = 0
    # #1723: true iff at least one fix cycle in the run produced a real
    # commit (OR'd across cycles) — distinguishes a genuinely-fixed cycle-0
    # blocker from a fix loop that converged (no MUST_FIX survivors) purely
    # because every cycle's codex fix invocation was a no-op. Advisory
    # optional field; defaults to None so payloads from producers that predate
    # this field remain explicitly unknown. Finalized fix-loop results always
    # populate a concrete bool.
    had_real_commit: bool | None = None


class AgentHealthEntry(BaseModel):
    """Phase C — per-agent health snapshot for orchestrator retry targeting (#174).

    Collected across all agents that ran during a pipeline (plan, impl,
    reviewers, fix-loop cycles, prep-pr) so the orchestrator can identify
    *which* agent caused a downgrade rather than just knowing that a downgrade
    occurred.

    ``scope`` mirrors the tier the agent was operating on; may be ``None`` for
    agents that don't have a scope concept (e.g. plan-reviewer). Free-form
    string rather than a closed ``ScopeTier`` enum — tolerate producer-side
    values outside ``{"small", "large"}`` rather than failing validation.
    """

    agent_id: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    scope: str | None = None

    # Why: mirrors the #953/#962 empty-item guard shape for a scalar field
    # (issue #1130). A blank agent_id defeats the orchestrator retry-targeting
    # use case this field exists for (§5.3).
    @field_validator("agent_id")
    @classmethod
    def _reject_blank_agent_id(cls, v: str) -> str:
        if _is_blank(v):
            msg = f"agent_id must be a non-empty, non-whitespace string (got {v!r})"
            raise ValueError(msg)
        return v


class Health(BaseModel):
    lowest_agent_confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    any_incomplete_risk: bool
    shortcuts: list[str] = Field(default_factory=list)
    recommendation: Literal["PROCEED", "EXIT_FOR_HUMAN_REVIEW"]
    downgrade_applied: bool = False
    fix_loop_escalated: bool = False
    # Phase C — per-agent breakdown so the orchestrator can target retries at
    # the specific agent that caused a downgrade (issue #174). Optional: absent
    # on payloads from older producers; defaults to empty list.
    agent_health_summary: list[AgentHealthEntry] = Field(default_factory=list)

    # Why: sibling of AutoDevResult's commits/friction_highlights/next_actions
    # guard (issue #1130) — shortcuts lives on Health, not AutoDevResult, so it
    # needs its own field_validator rather than joining the multi-field one.
    @field_validator("shortcuts")
    @classmethod
    def _reject_empty_shortcuts(cls, v: list[str]) -> list[str]:
        return _reject_empty_string_items(v, "shortcuts")


class Blocker(BaseModel):
    """Either an emitted blocker (``status=blocked``) or a synthetic one.

    ``reason`` is intentionally typed as ``str`` (open enum per §4.2). The
    producer may add new reasons without a schema bump; consumers surface
    unknown reasons verbatim.

    Phase B and Phase E of the queue-orchestrator observability expansion
    (issue #174) added five optional fields. All default to None so v1/v2
    blocks without them parse unchanged; producers emitting v3 should
    populate them per the headless-contract spec.
    """

    stage: str
    reason: str
    details: str = ""
    # Phase B — blocker context for orchestrator routing.
    exception_type: str | None = None
    message: str | None = None
    recovery_hint: str | None = None
    # Phase E — queue-aware retry semantics. ``retry_eligible=True`` paired
    # with a non-null ``retry_delay_seconds`` means the orchestrator can
    # safely re-dispatch after the given backoff. ``retry_eligible=False``
    # means human intervention is required.
    retry_eligible: bool | None = None
    retry_delay_seconds: int | None = None

    @model_validator(mode="after")
    def _check_retry_invariants(self) -> Blocker:
        # If retry_delay_seconds is set, retry_eligible must not be False.
        # retry_eligible=None means the field was omitted by an older producer
        # (issue #430 case 5) — treat as implied True when a delay is present.
        # retry_eligible=False with a delay is still a hard error.
        if self.retry_delay_seconds is not None and self.retry_eligible is False:
            msg = (
                "retry_delay_seconds set without retry_eligible=True "
                f"(got retry_eligible={self.retry_eligible!r})"
            )
            raise ValueError(msg)
        if self.retry_delay_seconds is not None and self.retry_eligible is None:
            # Older producer omitted retry_eligible; coerce to True so the
            # invariant is satisfied and the sentinel is not discarded.
            self.retry_eligible = True
        if self.retry_delay_seconds is not None and self.retry_delay_seconds < 0:
            msg = (
                f"retry_delay_seconds must be non-negative, "
                f"got {self.retry_delay_seconds}"
            )
            raise ValueError(msg)
        return self


_TERMINAL_REJECT_STATUSES: frozenset[Status] = frozenset(
    {
        "scope_exceeded",
        "forbidden_area",
        "blocked",
        "empty_diff_blocked",
        "stale_dispatch",
    },
)
_PRE_BRANCH_STATUSES: frozenset[Status] = frozenset(
    {
        "plan_pending_approval",
        "scope_exceeded",
        "forbidden_area",
        "no_op",
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    },
)
# Pre-flight + blocked is a retry/escalation shape, not a terminal reject —
# next_actions must signal the recovery verb. The Origin Sync block (#226)
# emits `sync_local_main`; `manual_intervention` covers escalation cases
# (e.g. local main has unmerged commits the orchestrator can't auto-resolve).
_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS: frozenset[str] = frozenset(
    {"sync_local_main", "manual_intervention"},
)
# next_actions prefixes that indicate a blocked session is paused for human
# input (issue #328). A blocked result carrying only these prefixes is not a
# terminal-reject shape — it will be re-dispatched once the human acts.
# Public so other modules can import and reuse the same list without duplicating.
USER_DIRECTED_PREFIXES: tuple[str, ...] = (
    "user_resolve_",
    "user_decide_",
    "user_verify_",
)


def _reject_empty_string_items(v: list[str], field_name: str) -> list[str]:
    """Raise if any item in *v* is empty/whitespace-only (issue #1130).

    Shared by AutoDevResult's commits/friction_highlights/next_actions
    multi-field validator and Health.shortcuts. Mirrors the indexed-message
    shape of _reject_empty_question_ambiguities/_reject_empty_claim_premises,
    adapted for bare string items rather than dict items.
    """
    for idx, item in enumerate(v):
        if _is_blank(item):
            msg = (
                f"{field_name}[{idx}] is an empty/whitespace-only string "
                f"(got {item!r}); every item must be a non-empty, non-whitespace "
                "string. Drop the empty item (see #1130)."
            )
            raise ValueError(msg)
    return v


def _has_usable_question(item: dict[str, Any]) -> bool:
    """Return True iff *item* carries a non-empty, non-whitespace question string."""
    q = item.get("question")
    return isinstance(q, str) and bool(q.strip())


def _has_usable_premise_text(item: dict[str, Any]) -> bool:
    """Return True iff *item* carries non-empty text under 'claim' or 'premise'."""
    for key in ("claim", "premise"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _is_resolved_premise(item: dict[str, Any]) -> bool:
    """Return True iff *item* is fully resolved (issue #1325).

    Resolved means BOTH: `verified` is the JSON boolean `True` -- strict, no
    truthy-string/int tolerance ("true", 1, etc. do NOT count; asymmetric
    risk favors under-matching over silently skipping a real human
    checkpoint) -- AND `resolution` is present as a non-empty, non-whitespace
    string naming the adopted/binding resolution the premise maps onto. The
    producer's own `resolves` key (quoted verbatim in issue #1325's evidence)
    is deliberately NOT accepted as an alias -- narrower is safer for a gate
    that removes a human checkpoint; only the documented `resolution` key
    (docs/headless-contract.md §4.4) counts. Both conditions independently
    gate: verified-only or resolution-only leaves the premise parked.
    """
    if item.get("verified") is not True:
        return False
    resolution = item.get("resolution")
    return isinstance(resolution, str) and bool(resolution.strip())


def _is_blank(s: str) -> bool:
    """Return True iff *s* is empty or whitespace-only."""
    return not s.strip()


class AutoDevResult(BaseModel):
    """Parsed sentinel block. All cross-field invariants from §3-§5 enforced."""

    schema_version: SchemaVersion
    ticket_id: str
    status: Status
    stage_reached: StageReached
    scope: Scope
    plan_source: PlanSource
    branch: str | None = None
    worktree_path: str | None = None
    fork_point_sha: str | None = None
    commits: list[str] = Field(default_factory=list)
    pr: PrInfo | None = None
    # Phase D — pre-merge PR snapshot emitted before auto-merge is triggered
    # (issue #174). Optional: absent on payloads from older producers. Non-null
    # when a PR was created during this pipeline run (i.e. status=shipped).
    pr_created: PrCreated | None = None
    review: Review
    health: Health
    friction_highlights: list[str] = Field(default_factory=list)
    blocker: Blocker | None = None
    next_actions: list[str] = Field(default_factory=list)
    # v4: populated when status is ambiguities_pending_resolution or
    # premises_pending_verification. Entry shapes are best-effort per §4.4 —
    # keys tolerate producer-side name drift, EXCEPT `question` (for
    # ambiguities), which must be a non-empty, non-whitespace string when an
    # item is present (issue #953, enforced by
    # _reject_empty_question_ambiguities), and the `claim`/`premise` union (for
    # premises), at least one of which must be a non-empty, non-whitespace
    # string when an item is present (issue #962, enforced by
    # _reject_empty_claim_premises).
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    # Total USD cost for this auto-dev run. Optional — producers that don't
    # track cost omit this field; consumers treat None as "cost unknown".
    # Must be non-negative when present. See GitHub issue #124.
    cost_usd: float | None = None
    # True iff this claim consumed an operator resolution during a plan-stage
    # `ambiguities_pending_resolution`/`premises_pending_verification` park,
    # with provenance recorded in resolution_evidence below. Optional —
    # producers that don't track this omit both fields; consumers
    # (cw.dispatch.productivity) treat a bare True with no evidence as not
    # credited. Typed `StrictBool` (not a `mode="before"` custom validator,
    # unlike `stage_reached`'s coercion-guard pattern) so Pydantic itself
    # rejects a coercible non-bool ("true"/1) rather than silently lax-mode
    # coercing it to True and defeating the consumer's identity check in
    # `productivity.py`. See GitHub issue #1896 R3.
    resolution_consumed: StrictBool = False
    # Provenance for resolution_consumed above: the settlement round's source
    # comment id/URL and the settled item ids. None when resolution_consumed
    # is False or absent. See GitHub issue #1896.
    resolution_evidence: dict[str, Any] | None = None

    @field_validator("cost_usd")
    @classmethod
    def _validate_cost_usd(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            msg = "cost_usd must be non-negative"
            raise ValueError(msg)
        return v

    @field_validator("resolution_evidence")
    @classmethod
    def _validate_resolution_evidence(
        cls, v: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if v is None:
            return v
        comment_id = v.get("comment_id")
        items = v.get("items")
        if not comment_id or not isinstance(items, list) or not items:
            msg = (
                "resolution_evidence must carry a non-empty 'comment_id' and a "
                "non-empty 'items' list (see #1896 R4)"
            )
            raise ValueError(msg)
        return v

    # Why: intentionally status-agnostic (fires on every model_validate, not
    # only ambiguities_pending_resolution) per #953 pre-flight resolution #1.
    # A stray populated `ambiguities` array with an empty-question item on an
    # unrelated status would now hard-fail as validation_failed instead of
    # being silently ignored — accepted trade-off, reviewed non-blocking by
    # Plan Soundness Reviewer at plan time (see .cw/deferred-findings.md).
    @field_validator("ambiguities")
    @classmethod
    def _reject_empty_question_ambiguities(
        cls, v: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for idx, item in enumerate(v):
            if not _has_usable_question(item):
                msg = (
                    f"ambiguities[{idx}] has an empty/missing 'question' "
                    f"(got {item.get('question')!r}); every ambiguity item must "
                    "carry a non-empty, non-whitespace question string. Drop the "
                    "empty item, or exit stage_complete if there is nothing to "
                    "ask (see #953)."
                )
                raise ValueError(msg)
        return v

    # Why: intentionally status-agnostic (fires on every model_validate, not
    # only premises_pending_verification) per #953 pre-flight resolution #1
    # (same rationale applied here, sibling of #953). A stray populated
    # `premises` array with an empty-claim item on an unrelated status would
    # now hard-fail as validation_failed instead of being silently ignored —
    # accepted trade-off, mirroring the ambiguities validator above.
    @field_validator("premises")
    @classmethod
    def _reject_empty_claim_premises(
        cls, v: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for idx, item in enumerate(v):
            if not _has_usable_premise_text(item):
                msg = (
                    f"premises[{idx}] has no usable 'claim'/'premise' text "
                    f"(got claim={item.get('claim')!r}, "
                    f"premise={item.get('premise')!r}); "
                    "every premise item must carry a non-empty, non-whitespace "
                    "string under 'claim' or 'premise'. Drop the empty item, "
                    "or exit stage_complete if there is nothing to verify "
                    "(see #962)."
                )
                raise ValueError(msg)
        return v

    # Why: intentionally status-agnostic, mirroring the #953/#962 validators
    # above (issue #1130). Unlike ambiguities/premises, none of these three
    # fields is gated behind a status-specific non-empty invariant — an empty
    # list remains each field's legitimate default/terminal value.
    @field_validator("commits", "friction_highlights", "next_actions")
    @classmethod
    def _reject_empty_string_list_fields(
        cls, v: list[str], info: ValidationInfo
    ) -> list[str]:
        return _reject_empty_string_items(v, str(info.field_name))

    @field_validator("stage_reached", mode="before")
    @classmethod
    def _normalize_stage_reached(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        if v in _STAGE_REACHED_ALIASES:
            return _STAGE_REACHED_ALIASES[v]
        if v in _STAGE_REACHED_CANONICAL:
            return v
        # Tolerant coercion for a near-miss within a known stage number (#748):
        # stage_reached is informational, so a stray label (e.g.
        # "stage4_pr_creation") must not fail the whole sentinel and discard
        # completed work. Genuine garbage (no stage<1-5> prefix) falls through
        # and rejects, preserving malformed-payload detection.
        for prefix, canonical in _STAGE_NUMBER_FALLBACK.items():
            if v.startswith(prefix):
                _log.warning(
                    "stage_reached %r is not canonical; coerced to %r by "
                    "stage-number prefix (#748)",
                    v,
                    canonical,
                )
                return canonical
        return v

    def _check_status_pairings(self) -> None:
        """§8/§3.3/§4.3/§5.1 status-coupled invariants (version, pr, blocker)."""
        # §8 status/version compat: v2-introduced statuses cannot ride on a
        # v1-tagged payload.
        if self.schema_version < _MIN_V2_SCHEMA_VERSION and self.status in _V2_STATUSES:
            msg = (
                f"status={self.status!r} requires schema_version>=2, "
                f"got {self.schema_version}"
            )
            raise ValueError(msg)

        # NOTE: ambiguities_pending_resolution and premises_pending_verification
        # (_V4_STATUSES) are NOT gated by schema_version. Spec §8 says enum
        # additions require a version bump (v4), and v4 IS the official home for
        # these values, BUT the producer skill emits them under v2 today (see
        # issue #316). One-time rollout exception: accept under v2, v3, AND v4
        # until the skill bumps. When skill emits v4, this exception can be
        # removed and the _V4_STATUSES gate re-added.

        # §3.3 pr: non-null iff status in {shipped, merge_pending} (#899)
        _pr_required_statuses = frozenset({"shipped", "merge_pending"})
        if self.status in _pr_required_statuses and self.pr is None:
            msg = f"pr must be non-null when status is {self.status!r}"
            raise ValueError(msg)
        if self.status not in _pr_required_statuses and self.pr is not None:
            msg = f"pr must be null when status is {self.status!r}"
            raise ValueError(msg)

        # §3.3 blocker: non-null iff status == blocked
        # Exception (issue #777): merge_gate_blocked may optionally carry a
        # non-null blocker to surface prior_pipeline_pr_open reason — backward
        # compat preserved since blocker=null is still accepted for this status.
        # Exception (#1870): empty_diff_blocked may carry one on the same terms
        # (EMPTY_DIFF_BLOCKER_REASON names which branch measured empty against
        # which base) — unlike scope_exceeded/forbidden_area it is post-branch,
        # so there is a real measurement to report.
        # Exception (#1862): stale_dispatch may carry one on the same terms
        # (STALE_DISPATCH_BLOCKER_REASON, with details naming the discovered
        # PR's number/URL/review state) -- that identity is the whole triage
        # signal, and `pr` stays required-null because this run did not create
        # the PR it found.
        if self.status == "blocked" and self.blocker is None:
            msg = "blocker must be non-null when status is 'blocked'"
            raise ValueError(msg)
        blocker_allowed = self.status in {
            "blocked",
            "merge_gate_blocked",
            "empty_diff_blocked",
            "stale_dispatch",
        }
        if not blocker_allowed and self.blocker is not None:
            msg = f"blocker must be null when status is {self.status!r}"
            raise ValueError(msg)

        # §4.3 next_actions: wait_for_ci iff shipped
        wait_present = "wait_for_ci" in self.next_actions
        if self.status == "shipped" and not wait_present:
            msg = (
                "'wait_for_ci' must be present in next_actions when status is 'shipped'"
            )
            raise ValueError(msg)
        if self.status != "shipped" and wait_present:
            msg = (
                f"'wait_for_ci' must not appear in next_actions "
                f"when status is {self.status!r}"
            )
            raise ValueError(msg)

        # §5.1 downgrade_applied implies review_pending_approval (tier relaxed —
        # issue #430 case 2: a producer reporting the original tier='large'
        # is now accepted; only the status constraint is enforced).
        if self.health.downgrade_applied and self.status != "review_pending_approval":
            msg = (
                "health.downgrade_applied=true requires "
                "status='review_pending_approval'"
            )
            raise ValueError(msg)

        # §4.1 merge_gate_blocked tier constraint relaxed (issue #430 case 3):
        # a large ticket that hit a merge gate was previously rejected.
        # The tier check is removed; any tier is now accepted for this status.

        # §3.3 pre-branch statuses must have branch=None
        if self.status in _PRE_BRANCH_STATUSES and self.branch is not None:
            msg = f"branch must be null when status is {self.status!r}"
            raise ValueError(msg)

    def _check_stage_invariants(self) -> None:
        """§3.3/§4.x stage-coupled invariants (pre-impl exits, pre-flight)."""
        # §3.3 lines_actual is None iff exited before impl (stage1_plan or
        # stage1_pre_flight — both exit before any implementation work).
        exited_pre_impl = self.stage_reached in ("stage1_plan", "stage1_pre_flight")
        if exited_pre_impl and self.scope.lines_actual is not None:
            msg = (
                "scope.lines_actual must be null when stage_reached is "
                "'stage1_plan' or 'stage1_pre_flight'"
            )
            raise ValueError(msg)
        if not exited_pre_impl and self.scope.lines_actual is None:
            msg = (
                "scope.lines_actual must be non-null when "
                f"stage_reached={self.stage_reached!r}"
            )
            raise ValueError(msg)

        # §3.3 scope.tier and health.lowest_agent_confidence are required at
        # post-impl stages but null-allowed at pre-impl exits (issue #416).
        if not exited_pre_impl and self.scope.tier is None:
            msg = (
                f"scope.tier must be non-null when stage_reached={self.stage_reached!r}"
            )
            raise ValueError(msg)
        if not exited_pre_impl and self.health.lowest_agent_confidence is None:
            msg = (
                "health.lowest_agent_confidence must be non-null when "
                f"stage_reached={self.stage_reached!r}"
            )
            raise ValueError(msg)

        # stage1_pre_flight can exit as no_op (work not needed), blocked
        # (work needed but a pre-flight gate failed, e.g. Origin Sync — see
        # issue #226), or stale_dispatch (work needed but this ticket already
        # has an open PR from an earlier dispatch — #1862; the intake
        # self-check that detects it runs at pre-flight, before any planning).
        # Other statuses still violate the pre-impl contract.
        if self.stage_reached == "stage1_pre_flight" and self.status not in (
            "no_op",
            "blocked",
            "stale_dispatch",
        ):
            msg = (
                f"stage_reached='stage1_pre_flight' requires status in "
                f"('no_op', 'blocked', 'stale_dispatch'), got status={self.status!r}"
            )
            raise ValueError(msg)

    def _check_next_actions_invariants(self) -> None:
        """§4.3/§4.4 next_actions and pending-array invariants."""
        pre_flight_blocked = (
            self.stage_reached == "stage1_pre_flight" and self.status == "blocked"
        )

        # Pre-flight + blocked is a retry/escalation shape: next_actions must
        # be non-empty and drawn from the allowed verb set. The generic
        # terminal-reject rule below (empty next_actions) does NOT apply here.
        if pre_flight_blocked:
            if not self.next_actions:
                msg = (
                    "next_actions must be non-empty when status='blocked' at "
                    "stage1_pre_flight (got empty list); expected one of "
                    f"{sorted(_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS)}"
                )
                raise ValueError(msg)
            invalid = [
                a
                for a in self.next_actions
                if a not in _PRE_FLIGHT_BLOCKED_NEXT_ACTIONS
            ]
            if invalid:
                msg = (
                    f"next_actions {invalid!r} not allowed for blocked at "
                    f"stage1_pre_flight; expected subset of "
                    f"{sorted(_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS)}"
                )
                raise ValueError(msg)

        # blocked + all-user-directed next_actions = paused for human input
        # (issue #328). Not a terminal-reject shape — will be re-dispatched.
        user_directed_blocked = (
            self.status == "blocked"
            and bool(self.next_actions)
            and all(a.startswith(USER_DIRECTED_PREFIXES) for a in self.next_actions)
        )

        # §4.3 terminal-reject statuses have empty next_actions, EXCEPT for
        # the pre-flight + blocked retry shape covered above, and the
        # user-directed blocked shape where all actions start with a user_*
        # prefix (issue #328).
        if (
            self.status in _TERMINAL_REJECT_STATUSES
            and self.next_actions
            and not pre_flight_blocked
            and not user_directed_blocked
        ):
            msg = (
                f"next_actions must be empty for terminal-reject status "
                f"{self.status!r}, got {self.next_actions!r}"
            )
            raise ValueError(msg)

        # §4.3 (A2) v4 pending statuses require non-empty next_actions.
        if self.status in _V4_STATUSES and not self.next_actions:
            msg = f"next_actions must be non-empty when status is {self.status!r}"
            raise ValueError(msg)

        # §4.4 (A5) cross-field array invariants: arrays must be non-empty
        # when their corresponding status is set (empty array is a producer bug
        # — nothing for the consumer to act on).
        if self.status == "ambiguities_pending_resolution" and not self.ambiguities:
            msg = (
                "ambiguities must be non-empty when "
                "status='ambiguities_pending_resolution'"
            )
            raise ValueError(msg)
        if self.status == "premises_pending_verification" and not self.premises:
            msg = (
                "premises must be non-empty when status='premises_pending_verification'"
            )
            raise ValueError(msg)

    @model_validator(mode="after")
    def _check_invariants(self) -> AutoDevResult:
        self._check_status_pairings()
        self._check_stage_invariants()
        self._check_next_actions_invariants()
        return self


class BlockedResult(BaseModel):
    """Synthetic result for §6 failure modes (parser-side blockers).

    Distinct from a producer-emitted ``AutoDevResult`` with ``status=blocked``:
    a ``BlockedResult`` indicates that the parser could not extract a valid
    sentinel payload at all. cw should treat these the same as a real
    ``blocked`` outcome — surface to user, do not auto-route.
    """

    status: Literal["blocked"] = "blocked"
    blocker: Blocker
