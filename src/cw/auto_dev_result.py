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

from pydantic import BaseModel, Field, ValidationError, model_validator

_log = logging.getLogger(__name__)

# v1 is the legacy shape; v2 adds the `no_op` status; v3 adds the
# `stage1_pre_flight` stage_reached value and `none` plan_source (used
# together for pre-flight no_op exits). All are accepted during the rollout
# window — see docs/headless-contract.md §8.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2, 3})

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
]
# Statuses introduced after v1. Emitting one under schema_version=1 is a
# producer bug — it would silently degrade for downstream tools that key off
# the version field.
_V2_STATUSES: frozenset[str] = frozenset({"no_op"})
# NOTE: stage1_pre_flight (StageReached) and "none" (PlanSource) are NOT
# gated by schema_version. Spec §8 says enum additions require a version
# bump (v3), and v3 IS the official home for these values, BUT the producer
# skill emits them under v2 today (see #103). One-time rollout exception:
# accept under v2 AND v3 until the skill bumps. When skill emits v3, this
# exception can be removed and a `_V3_STAGES`/`_V3_PLAN_SOURCES` gate added.
StageReached = Literal[
    "stage1_pre_flight",
    "stage1_plan",
    "stage2_impl",
    "stage3_review",
    "stage4a_merge_gate",
    "stage4b_pr_create",
    "stage5_post_create",
]
ScopeTier = Literal["small", "large"]
PlanSource = Literal["linear_existing", "generated", "free_text", "none"]


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


class Review(BaseModel):
    must_fix_initial: int
    should_fix: int
    fix_cycles_used: int


class Health(BaseModel):
    lowest_agent_confidence: Literal["HIGH", "MEDIUM", "LOW"]
    any_incomplete_risk: bool
    shortcuts: list[str] = Field(default_factory=list)
    recommendation: Literal["PROCEED", "EXIT_FOR_HUMAN_REVIEW"]
    downgrade_applied: bool = False
    fix_loop_escalated: bool = False


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
    {"plan_pending_approval", "scope_exceeded", "forbidden_area", "no_op"},
)


class AutoDevResult(BaseModel):
    """Parsed sentinel block. All cross-field invariants from §3-§5 enforced."""

    schema_version: Literal[1, 2, 3]
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
    review: Review
    health: Health
    friction_highlights: list[str] = Field(default_factory=list)
    blocker: Blocker | None = None
    next_actions: list[str] = Field(default_factory=list)

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

        # stage1_pre_flight can only exit as no_op (pre-flight exits before any
        # plan is produced — other statuses are not possible here).
        if self.stage_reached == "stage1_pre_flight" and self.status != "no_op":
            msg = (
                f"stage_reached='stage1_pre_flight' requires status='no_op', "
                f"got status={self.status!r}"
            )
            raise ValueError(msg)

        # §4.3 terminal-reject statuses have empty next_actions
        if self.status in _TERMINAL_REJECT_STATUSES and self.next_actions:
            msg = (
                f"next_actions must be empty for terminal-reject status "
                f"{self.status!r}, got {self.next_actions!r}"
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
        # §6 (1) no sentinel, or §6 (2) opening without close. We don't
        # distinguish — both indicate the skill failed before/during the
        # emit step.
        if _OPEN_SENTINEL in text:
            reason = "no_result_emitted"
            details = f"opening sentinel present, close missing; tail:\n{_tail(text)}"
        else:
            reason = "no_result_emitted"
            details = f"no sentinel block in stdout; tail:\n{_tail(text)}"
        return BlockedResult(
            blocker=Blocker(stage="unknown", reason=reason, details=details),
        )

    raw_block = matches[0].group(1)

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
