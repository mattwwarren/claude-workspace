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
# Statuses introduced in v4 (issue #191). Per rollout exception (issue #316),
# accepted under all supported schema versions (v2, v3, v4) until the producer
# skill bumps its emitted schema_version to v4.
_V4_STATUSES: frozenset[str] = frozenset(
    {"ambiguities_pending_resolution", "premises_pending_verification"}
)
# Public alias for consumers that need to check whether a status indicates the
# session is paused waiting for human input (issue #129).
PAUSED_FOR_USER_INPUT_STATUSES: frozenset[str] = _V4_STATUSES

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
ScopeTier = Literal["small", "large"]
PlanSource = Literal[
    "linear_existing",
    "github_issue_existing",
    "generated",
    "free_text",
    "none",
]


class Scope(BaseModel):
    tier: ScopeTier
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
    lowest_agent_confidence: Literal["HIGH", "MEDIUM", "LOW"]
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
        # If retry_delay_seconds is set, retry_eligible must be True. The
        # reverse is allowed — a producer can mark retry_eligible without
        # committing to a specific backoff.
        if self.retry_delay_seconds is not None and self.retry_eligible is not True:
            msg = (
                "retry_delay_seconds set without retry_eligible=True "
                f"(got retry_eligible={self.retry_eligible!r})"
            )
            raise ValueError(msg)
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
# Public so wrapper.py can import and reuse the same list without duplicating.
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
        if isinstance(v, str) and v in _STAGE_REACHED_ALIASES:
            return _STAGE_REACHED_ALIASES[v]
        return v

    @model_validator(mode="after")
    def _check_invariants(self) -> AutoDevResult:
        # §8 status/version compat: v2-introduced statuses cannot ride on a
        # v1-tagged payload.
        if self.schema_version < 2 and self.status in _V2_STATUSES:
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

        # §5.1 downgrade_applied implies review_pending_approval + small
        if self.health.downgrade_applied and (
            self.status != "review_pending_approval" or self.scope.tier != "small"
        ):
            msg = (
                "health.downgrade_applied=true requires "
                "status='review_pending_approval' and scope.tier='small'"
            )
            raise ValueError(msg)

        # §4.1 merge_gate_blocked is small-scope only
        if self.status == "merge_gate_blocked" and self.scope.tier != "small":
            msg = "merge_gate_blocked requires scope.tier='small'"
            raise ValueError(msg)

        # §3.3 pre-branch statuses must have branch=None
        if self.status in _PRE_BRANCH_STATUSES and self.branch is not None:
            msg = f"branch must be null when status is {self.status!r}"
            raise ValueError(msg)

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

        # stage1_pre_flight can exit as no_op (work not needed) or blocked
        # (work needed but a pre-flight gate failed, e.g. Origin Sync — see
        # issue #226). Other statuses still violate the pre-impl contract.
        pre_flight_blocked = (
            self.stage_reached == "stage1_pre_flight" and self.status == "blocked"
        )
        if self.stage_reached == "stage1_pre_flight" and self.status not in (
            "no_op",
            "blocked",
        ):
            msg = (
                f"stage_reached='stage1_pre_flight' requires status in "
                f"('no_op', 'blocked'), got status={self.status!r}"
            )
            raise ValueError(msg)

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


def parse_stdout(text: str) -> AutoDevResult | BlockedResult:
    """Parse a worker's stdout and return either the result or a synthetic blocker.

    Handles all six §6 failure modes by returning a :class:`BlockedResult`
    rather than raising. Callers can branch on ``isinstance(result,
    AutoDevResult)`` or check ``result.status``.
    """
    # §6 (6) multi-block detection comes first: even if the LAST block is
    # well-formed, the contract says exactly one per invocation.
    matches = list(_BLOCK_RE.finditer(text))
    if len(matches) > 1:
        last_payload = matches[-1].group(1)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="multiple_result_blocks",
                details=f"count={len(matches)}; last_block={last_payload}",
            ),
        )

    if not matches:
        if _OPEN_SENTINEL in text:
            # §6 (2) opening sentinel present, close missing — skill crashed mid-emit
            return BlockedResult(
                blocker=Blocker(
                    stage="unknown",
                    reason="no_result_emitted",
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
                    reason="no_result_emitted",
                    details=f"no sentinel block in stdout; tail:\n{_tail(text)}",
                ),
            )
        _log.warning(
            "auto-dev: sentinel emitted as bare code-fenced JSON without "
            "AUTO_DEV_RESULT markers; using loose fallback (GitHub #337)"
        )
        raw_block = loose_json
    else:
        raw_block = _strip_code_fence(matches[0].group(1))

    # §6 (3) JSON does not parse.
    try:
        payload: Any = json.loads(raw_block)
    except json.JSONDecodeError as exc:
        _log.warning("auto-dev sentinel block did not parse as JSON: %s", exc)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="no_result_emitted",
                details=f"sentinel block JSON parse failed ({exc}); raw:\n{raw_block}",
            ),
        )

    if not isinstance(payload, dict):
        type_name = type(payload).__name__
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="no_result_emitted",
                details=f"sentinel block was not a JSON object (got {type_name})",
            ),
        )

    # §6 (4) schema_version higher than supported, and the related case where
    # the field is missing or non-int. Pre-validate before handing to Pydantic
    # so the caller gets a structured surface instead of a ValidationError.
    raw_version = payload.get("schema_version")
    if not isinstance(raw_version, int) or raw_version not in SUPPORTED_SCHEMA_VERSIONS:
        _log.warning(
            "auto-dev sentinel schema_version=%r unsupported (parser supports %s)",
            raw_version,
            sorted(SUPPORTED_SCHEMA_VERSIONS),
        )
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="schema_version_unsupported",
                details=(
                    f"got schema_version={raw_version!r}, "
                    f"parser supports {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
                ),
            ),
        )

    # §6 (5) unknown status — short-circuit before Pydantic raises a
    # ValidationError on the closed Literal.
    raw_status = payload.get("status")
    if raw_status not in {
        "shipped",
        "plan_pending_approval",
        "review_pending_approval",
        "merge_gate_blocked",
        "scope_exceeded",
        "forbidden_area",
        "blocked",
        "no_op",
        "ambiguities_pending_resolution",
        "premises_pending_verification",
    }:
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="status_unknown",
                details=(
                    f"got status={raw_status!r}; surface verbatim, do not auto-route"
                ),
            ),
        )

    # Pre-validation normalization for no_op + stray pr/branch/commits (issue
    # #367). The producer sometimes emits status=no_op alongside a non-null pr
    # or branch when the pipeline ran far enough to create a branch/PR before
    # determining no work was needed. AutoDevResult._check_invariants (§3.3)
    # correctly rejects this shape; leniency here applies only at the stdout-
    # parse boundary where producer drift is expected. Does NOT apply to
    # shipped or blocked — those contradictions are genuinely ambiguous and
    # should still fail loudly.
    if raw_status == "no_op":
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
        if stray:
            _log.warning(
                "auto-dev: no_op sentinel carried non-null %s; coercing to clean "
                "no_op (ticket=%s, schema_version=%s)",
                stray,
                payload.get("ticket_id", "unknown"),
                payload.get("schema_version"),
            )

    # Pre-validation normalization for blocked + stray next_actions (issue
    # #371 — follow-up to #367/#370). A producer bug emitted status=blocked
    # alongside next_actions=['redispatch_ticket'] (or similar non-user-directed
    # verbs). The §4.3 terminal-reject invariant rejects the whole sentinel as
    # validation_failed, masking the real blocker. Coerce: drop stray
    # next_actions, preserve the original blocker intact. Leniency applies only
    # at the parse boundary (same scoping as the no_op coerce above).
    # Two legitimate shapes carry next_actions on blocked and MUST NOT be coerced:
    #   - pre-flight blocked (stage_reached='stage1_pre_flight'), and
    #   - user-directed blocked (all next_actions start with user_* prefixes).
    if raw_status == "blocked":
        raw_next_actions = payload.get("next_actions")
        if isinstance(raw_next_actions, list) and raw_next_actions:
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

    try:
        return AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        _log.warning("auto-dev sentinel failed model validation: %s", exc)
        return BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="validation_failed",
                details=f"{exc}",
            ),
        )
