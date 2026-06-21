"""Parser for the ``<<<AUTO_DEV_RESULT`` sentinel block.

The headless ``/auto-dev`` skill emits a sentinel-delimited JSON block as the
final lines of stdout summarizing the pipeline outcome. ``cw`` parses that
block to persist a structured view on the worker Session.

Spec: ``docs/headless-contract.md`` (§3 framing, §4 enum, §5 health, §6
failure modes).

Public surface:

- :class:`AutoDevResult` and its nested models (Pydantic).
- :func:`parse_stdout` — accepts raw stdout, returns either a parsed
  ``AutoDevResult`` or a synthetic ``BlockedResult`` describing why the
  payload was unusable. Never raises on malformed input.
- :func:`extract_block` — low-level helper that locates the LAST sentinel
  pair and returns the inner JSON text (no parsing).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

_log = logging.getLogger(__name__)

# v1 is the legacy shape; v2 adds the `no_op` status; v3 adds the
# `stage1_pre_flight` stage_reached value and `none` plan_source (used
# together for pre-flight no_op exits); v4 promotes
# `ambiguities_pending_resolution` and `premises_pending_verification` to
# canonical closed-enum statuses (issue #191). All are accepted during the
# rollout window — see docs/headless-contract.md §8.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2, 3, 4})
AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION: int = max(SUPPORTED_SCHEMA_VERSIONS)

_OPEN_SENTINEL = "<<<AUTO_DEV_RESULT"
_CLOSE_SENTINEL = "AUTO_DEV_RESULT>>>"
# A "complete" block runs from a line containing the open sentinel to a
# subsequent line containing only the close sentinel. We take the LAST such
# block per §3.1 — narrative above the real block may legitimately quote
# the literal sentinel string (e.g., this docstring).
_BLOCK_RE = re.compile(
    r"<<<AUTO_DEV_RESULT\s*\n(.*?)\nAUTO_DEV_RESULT>>>",
    re.DOTALL,
)
# Fallback locator for sentinels emitted as bare code-fenced JSON without
# AUTO_DEV_RESULT markers (GitHub #337). Matches ```json or ``` fenced blocks.
_LOOSE_FENCE_RE = re.compile(
    r"```(?:json)?\n(.*?)\n```",
    re.DOTALL,
)

# Keep the last-N-lines payload bounded so synthetic blocker details don't
# bloat the persisted state file. 40 lines is enough to capture a typical
# pre-crash traceback without dragging in megabytes of pane scrollback.
_TAIL_LINES = 40


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
    "scope_exceeded",
    "forbidden_area",
    "blocked",
    "no_op",
    "ambiguities_pending_resolution",
    "premises_pending_verification",
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
    {"blocked", "merge_gate_blocked", "scope_exceeded", "forbidden_area"}
)
SCOPE_TIER_SMALL: Literal["small"] = "small"

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
            "scope_exceeded",
            "forbidden_area",
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

# BlockedResult reason codes produced by parse_stdout.  Exported so consumers
# (e.g. cli.py) can reference them without duplicating the literal strings.
BLOCKER_REASON_MULTIPLE_RESULT_BLOCKS = "multiple_result_blocks"
BLOCKER_REASON_NO_RESULT_EMITTED = "no_result_emitted"
BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
BLOCKER_REASON_STATUS_UNKNOWN = "status_unknown"
BLOCKER_REASON_VALIDATION_FAILED = "validation_failed"

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
    {"scope_exceeded", "forbidden_area", "blocked"},
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


class AutoDevResult(BaseModel):
    """Parsed sentinel block. All cross-field invariants from §3-§5 enforced."""

    schema_version: Literal[1, 2, 3, 4]
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
    # all keys optional, tolerate producer-side key-name drift.
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    # Total USD cost for this auto-dev run. Optional — producers that don't
    # track cost omit this field; consumers treat None as "cost unknown".
    # Must be non-negative when present. See GitHub issue #124.
    cost_usd: float | None = None

    @field_validator("cost_usd")
    @classmethod
    def _validate_cost_usd(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            msg = "cost_usd must be non-negative"
            raise ValueError(msg)
        return v

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

        # §3.3 pr: non-null iff status == shipped
        if self.status == "shipped" and self.pr is None:
            msg = "pr must be non-null when status is 'shipped'"
            raise ValueError(msg)
        if self.status != "shipped" and self.pr is not None:
            msg = f"pr must be null when status is {self.status!r}"
            raise ValueError(msg)

        # §3.3 blocker: non-null iff status == blocked
        if self.status == "blocked" and self.blocker is None:
            msg = "blocker must be non-null when status is 'blocked'"
            raise ValueError(msg)
        if self.status != "blocked" and self.blocker is not None:
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

        # stage1_pre_flight can exit as no_op (work not needed) or blocked
        # (work needed but a pre-flight gate failed, e.g. Origin Sync — see
        # issue #226). Other statuses still violate the pre-impl contract.
        if self.stage_reached == "stage1_pre_flight" and self.status not in (
            "no_op",
            "blocked",
        ):
            msg = (
                f"stage_reached='stage1_pre_flight' requires status in "
                f"('no_op', 'blocked'), got status={self.status!r}"
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


# Placeholder field values from the documented illustrative example in the
# /auto-dev skill prompt. A sentinel matching all three is the example block,
# not a real result. Multiple fields required — never reject on pr.number==42
# alone (a real first PR can legitimately be #42).
_EXAMPLE_PR_NUMBER = 42
_EXAMPLE_TICKET_ID = "PROJ-1234"
_EXAMPLE_BRANCH_PREFIX = "dev/proj-1234"


def is_documented_example(result: AutoDevResult) -> bool:
    """Return True iff *result* matches the illustrative example in the skill prompt.

    Used by transcript scanners to skip the example sentinel block when the
    worker quotes it before emitting the real result (GitHub #591).
    """
    return (
        result.pr is not None
        and result.pr.number == _EXAMPLE_PR_NUMBER
        and result.ticket_id == _EXAMPLE_TICKET_ID
        and result.branch is not None
        and result.branch.startswith(_EXAMPLE_BRANCH_PREFIX)
    )


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence wrapper from a sentinel block payload.

    Only strips known-safe language specs (json or plain). Unknown specs
    (e.g. typescript) and missing closing fences are left for json.loads
    to reject loudly.
    """
    for prefix in ("```json\n", "```\n"):
        if raw.startswith(prefix) and raw.endswith("\n```"):
            return raw[len(prefix) : -4]
    return raw


def _extract_loose_sentinel_json(text: str) -> str | None:
    """Scan for the last code-fenced block that parses as an auto-dev payload.

    Used as a fallback when ``parse_stdout`` finds no AUTO_DEV_RESULT markers
    (GitHub #337 — producer occasionally emits the payload in a code fence
    without the sentinel framing). Accepts only blocks whose inner JSON is a
    dict containing both ``schema_version`` and ``status`` keys, distinguishing
    an auto-dev result from unrelated code blocks in the output.
    """
    for m in reversed(list(_LOOSE_FENCE_RE.finditer(text))):
        candidate = m.group(1).strip()
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "schema_version" in obj and "status" in obj:
            return candidate
    return None


def extract_block(text: str) -> str | None:
    """Return the JSON text inside the LAST complete sentinel pair, or None.

    Does not parse the JSON. Returns None if no complete pair is found —
    callers must distinguish "no opening sentinel at all" from "opening
    present but no close" themselves if they care (see :func:`parse_stdout`).
    """
    matches = list(_BLOCK_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1)


_KNOWN_STATUSES: frozenset[str] = frozenset(
    {
        "shipped",
        "stage_complete",
        "plan_pending_approval",
        "review_pending_approval",
        "merge_gate_blocked",
        "scope_exceeded",
        "forbidden_area",
        "blocked",
        "no_op",
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    }
)
_PRE_IMPL_STAGES: frozenset[str] = frozenset({"stage1_pre_flight", "stage1_plan"})


def _effective_stage(payload: dict[str, Any]) -> object:
    """Resolve ``stage_reached`` through the alias table (raw value if no alias)."""
    raw_stage = payload.get("stage_reached", "")
    if isinstance(raw_stage, str):
        return _STAGE_REACHED_ALIASES.get(raw_stage, raw_stage)
    return raw_stage


def _locate_raw_block(text: str) -> str | BlockedResult:
    """Locate the single sentinel payload in *text* or describe why it's unusable.

    Returns the inner JSON text (sentinel-framed or loose code-fenced fallback)
    or a :class:`BlockedResult` for §6 (1), (2), and (6) framing failures.
    """
    # §6 (6) multi-block detection comes first: even if the LAST block is
    # well-formed, the contract says exactly one per invocation.
    matches = list(_BLOCK_RE.finditer(text))
    if len(matches) > 1:
        last_payload = matches[-1].group(1)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_MULTIPLE_RESULT_BLOCKS,
                details=f"count={len(matches)}; last_block={last_payload}",
            ),
        )

    if matches:
        return _strip_code_fence(matches[0].group(1))

    if _OPEN_SENTINEL in text:
        # §6 (2) opening sentinel present, close missing — skill crashed mid-emit
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=(
                    f"opening sentinel present, close missing; tail:\n{_tail(text)}"
                ),
            ),
        )

    # §6 (1) No AUTO_DEV_RESULT markers. Tolerate bare code-fenced JSON:
    # the producer occasionally emits the payload in a ``` block without
    # sentinel framing (GitHub #337). Accept iff the last fenced block
    # parses as a JSON object with both schema_version and status.
    loose_json = _extract_loose_sentinel_json(text)
    if loose_json is None:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"no sentinel block in stdout; tail:\n{_tail(text)}",
            ),
        )
    _log.warning(
        "auto-dev: sentinel emitted as bare code-fenced JSON without "
        "AUTO_DEV_RESULT markers; using loose fallback (GitHub #337)"
    )
    return loose_json


def _decode_payload(raw_block: str) -> dict[str, Any] | BlockedResult:
    """Decode the sentinel JSON and pre-validate version/status (§6 (3)-(5)).

    Returns the payload dict on success or a :class:`BlockedResult` for the
    parse/shape/version/status failure modes.
    """
    # §6 (3) JSON does not parse.
    try:
        payload: Any = json.loads(raw_block)
    except json.JSONDecodeError as exc:
        _log.warning("auto-dev sentinel block did not parse as JSON: %s", exc)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"sentinel block JSON parse failed ({exc}); raw:\n{raw_block}",
            ),
        )

    if not isinstance(payload, dict):
        type_name = type(payload).__name__
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_NO_RESULT_EMITTED,
                details=f"sentinel block was not a JSON object (got {type_name})",
            ),
        )

    # §6 (4) schema_version higher than supported, and the related case where
    # the field is missing or non-int. Pre-validate before handing to Pydantic
    # so the caller gets a structured surface instead of a ValidationError.
    raw_version = payload.get("schema_version")
    _max_supported = max(SUPPORTED_SCHEMA_VERSIONS)

    if isinstance(raw_version, int) and raw_version == _max_supported + 1:
        # One-version look-ahead: schema-bump PR self-shipped while the running
        # parser is still at N. Best-effort parse with the current max schema so
        # the shipped result is recognised rather than mis-flagged as a failure.
        # (issue #395 / headless-contract.md §6(4))
        _log.warning(
            "auto-dev sentinel schema_version=%r is one ahead of parser max=%r; "
            "best-effort parse using schema %r (schema-bump skew tolerance)",
            raw_version,
            _max_supported,
            _max_supported,
        )
        payload["schema_version"] = _max_supported

    elif (
        not isinstance(raw_version, int) or raw_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        _log.warning(
            "auto-dev sentinel schema_version=%r unsupported (parser supports %s)",
            raw_version,
            sorted(SUPPORTED_SCHEMA_VERSIONS),
        )
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
                details=(
                    f"got schema_version={raw_version!r}, "
                    f"parser supports {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
                ),
            ),
        )

    # §6 (5) unknown status — short-circuit before Pydantic raises a
    # ValidationError on the closed Literal.
    raw_status = payload.get("status")
    if raw_status not in _KNOWN_STATUSES:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_STATUS_UNKNOWN,
                details=(
                    f"got status={raw_status!r}; surface verbatim, do not auto-route"
                ),
            ),
        )

    return payload


def _coerce_no_op_strays(payload: dict[str, Any]) -> None:
    """Drop stray pr/branch/commits/lines_actual on a no_op payload (issue #367)."""
    stray: list[str] = []
    if payload.get("pr") is not None:
        stray.append("pr")
        payload["pr"] = None
    if payload.get("branch") is not None:
        stray.append("branch")
        payload["branch"] = None
    if payload.get("commits"):
        stray.append("commits")
        payload["commits"] = []
    # Coerce stray scope.lines_actual on pre-impl exits (issue #399).
    # A no_op at stage1_pre_flight or stage1_plan exited before any
    # implementation work; lines_actual must be null. The producer
    # sometimes emits a non-null value, tripping the §3.3 cross-field
    # invariant and causing the sentinel to fail as validation_failed.
    scope_dict = payload.get("scope")
    if (
        isinstance(scope_dict, dict)
        and scope_dict.get("lines_actual") is not None
        and _effective_stage(payload) in _PRE_IMPL_STAGES
    ):
        stray.append("scope.lines_actual")
        scope_dict["lines_actual"] = None
    if stray:
        _log.warning(
            "auto-dev: no_op sentinel carried non-null %s; coercing to clean "
            "no_op (ticket=%s, schema_version=%s)",
            stray,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )


def _coerce_terminal_strays(payload: dict[str, Any], raw_status: str) -> None:
    """Drop stray branch/commits/lines_actual on scope_exceeded/forbidden_area.

    Issue #430 case 4. Post-impl stages require non-null lines_actual per §3.3;
    lines_actual is only coerced on pre-impl exits (same rule as no_op).
    """
    stray_term: list[str] = []
    if payload.get("branch") is not None:
        stray_term.append("branch")
        payload["branch"] = None
    if payload.get("commits"):
        stray_term.append("commits")
        payload["commits"] = []
    scope_dict_term = payload.get("scope")
    if (
        isinstance(scope_dict_term, dict)
        and scope_dict_term.get("lines_actual") is not None
        and _effective_stage(payload) in _PRE_IMPL_STAGES
    ):
        stray_term.append("scope.lines_actual")
        scope_dict_term["lines_actual"] = None
    if stray_term:
        _log.warning(
            "auto-dev: %s sentinel carried non-null %s; coercing to clean "
            "%s (ticket=%s, schema_version=%s)",
            raw_status,
            stray_term,
            raw_status,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )


def _coerce_empty_pending_array(
    payload: dict[str, Any], key: str, raw_status: str
) -> None:
    """Inject a minimal placeholder for an empty ambiguities/premises array.

    Issue #430 case 1 — accept empty arrays at the parse boundary so the §4.4
    A5 invariant does not turn producer drift into validation_failed.
    """
    if not payload.get(key):  # None or [] both need coercing
        _log.warning(
            "auto-dev: %s sentinel has empty %s; coercing to minimal "
            "placeholder (ticket=%s, schema_version=%s)",
            raw_status,
            key,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )
        payload[key] = [{}]


def _coerce_blocked_next_actions(payload: dict[str, Any]) -> None:
    """Drop stray next_actions on a blocked payload, preserving the blocker.

    Issue #371. Two legitimate shapes carry next_actions on blocked and MUST
    NOT be coerced: pre-flight blocked (stage_reached='stage1_pre_flight'), and
    user-directed blocked (all next_actions start with user_* prefixes).
    """
    raw_next_actions = payload.get("next_actions")
    if not (isinstance(raw_next_actions, list) and raw_next_actions):
        return
    is_pre_flight = payload.get("stage_reached") == "stage1_pre_flight"
    is_user_directed = all(
        isinstance(a, str) and a.startswith(USER_DIRECTED_PREFIXES)
        for a in raw_next_actions
    )
    if not is_pre_flight and not is_user_directed:
        _log.warning(
            "auto-dev: blocked sentinel carried stray next_actions=%r; "
            "dropping next_actions, preserving blocker "
            "(ticket=%s, schema_version=%s)",
            raw_next_actions,
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )
        payload["next_actions"] = []


def _coerce_pre_impl_zero_lines(payload: dict[str, Any]) -> None:
    """Coerce lines_actual=0 to null on pre-impl stages (issue #416).

    Only integer 0 is coerced; any other non-null value stays intact (hard
    error per §3.3). Status-agnostic, unlike the no_op coerce.
    """
    scope_gen = payload.get("scope")
    if (
        isinstance(scope_gen, dict)
        and _effective_stage(payload) in _PRE_IMPL_STAGES
        and scope_gen.get("lines_actual") == 0
    ):
        _log.warning(
            "auto-dev: pre-impl sentinel had lines_actual=0; coercing to null "
            "(ticket=%s, schema_version=%s)",
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )
        scope_gen["lines_actual"] = None


def _coerce_shipped_wait_for_ci(payload: dict[str, Any]) -> None:
    """Inject wait_for_ci on a shipped payload that omits it (issue #417)."""
    na = payload.get("next_actions")
    if isinstance(na, list) and "wait_for_ci" not in na:
        _log.warning(
            "auto-dev: shipped sentinel missing wait_for_ci; injecting "
            "(ticket=%s, schema_version=%s)",
            payload.get("ticket_id", "unknown"),
            payload.get("schema_version"),
        )
        payload["next_actions"] = [*na, "wait_for_ci"]


def _normalize_payload(payload: dict[str, Any], raw_status: str) -> None:
    """Apply all parse-boundary leniency coercions in place (producer drift).

    Each coercion is a documented, status-gated relaxation of a §3/§4 invariant
    that the strict ``model_validate`` still enforces. See the individual
    ``_coerce_*`` helpers for the per-issue rationale.
    """
    if raw_status == "no_op":
        _coerce_no_op_strays(payload)
    if raw_status in ("scope_exceeded", "forbidden_area"):
        _coerce_terminal_strays(payload, raw_status)
    if raw_status == "ambiguities_pending_resolution":
        _coerce_empty_pending_array(payload, "ambiguities", raw_status)
    if raw_status == "premises_pending_verification":
        _coerce_empty_pending_array(payload, "premises", raw_status)
    if raw_status == "blocked":
        _coerce_blocked_next_actions(payload)
    # Status-agnostic: applies regardless of raw_status (distinct from above).
    _coerce_pre_impl_zero_lines(payload)
    if raw_status == "shipped":
        _coerce_shipped_wait_for_ci(payload)


def parse_stdout(text: str) -> AutoDevResult | BlockedResult:
    """Parse a worker's stdout and return either the result or a synthetic blocker.

    Handles all six §6 failure modes by returning a :class:`BlockedResult`
    rather than raising. Callers can branch on ``isinstance(result,
    AutoDevResult)`` or check ``result.status``.
    """
    located = _locate_raw_block(text)
    if isinstance(located, BlockedResult):
        return located

    decoded = _decode_payload(located)
    if isinstance(decoded, BlockedResult):
        return decoded

    payload = decoded
    raw_status = payload["status"]
    _normalize_payload(payload, raw_status)

    try:
        return AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        _log.warning("auto-dev sentinel failed model validation: %s", exc)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_VALIDATION_FAILED,
                details=f"{exc}",
            ),
        )
