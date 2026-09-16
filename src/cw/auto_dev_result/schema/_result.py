"""The :class:`AutoDevResult` sentinel model and its cross-field invariants.

The top-level payload model every ``<<<AUTO_DEV_RESULT`` block decodes into,
plus :class:`BlockedResult` (the synthetic §6 parser-side failure shape) and
the private status/next_actions sets its validators read. All §3-§5 cross-field
invariants that span more than one nested model are enforced here.

Spec: ``docs/headless-contract.md`` (§3 framing, §4 enum, §5 health, §6 failure
modes). Package split: issue #2193.
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

from cw.auto_dev_result.schema._models import (
    Blocker,
    Health,
    PrCreated,
    PrInfo,
    Review,
    Scope,
)
from cw.auto_dev_result.schema._validators import (
    _has_usable_premise_text,
    _has_usable_question,
    _reject_empty_string_items,
)
from cw.auto_dev_result.schema._vocab import (
    _MIN_V2_SCHEMA_VERSION,
    _STAGE_NUMBER_FALLBACK,
    _STAGE_REACHED_ALIASES,
    _STAGE_REACHED_CANONICAL,
    _V2_STATUSES,
    _V4_STATUSES,
    PlanSource,
    SchemaVersion,
    StageReached,
    Status,
)

_log = logging.getLogger(__name__)


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
    #
    # #2098: this is scoped to Step 1c.0 settlement ONLY — a resumed round
    # that transcribes an operator's reply to a parked ambiguity/premise via
    # its own step 5 candidate. A Step 1b `## Binding Pre-flight Resolutions`
    # merge (an operator comment folded into the plan-agent prompt before the
    # plan is even generated) is a *different* mechanism and never mints a
    # `resolution_evidence` candidate, even though it is also an "operator
    # resolution" in the colloquial sense — its trace lives in the plan's own
    # `## Pre-flight Resolution Conformance` section and `friction_highlights`,
    # not here. Widening this field to cover every merged pre-flight
    # resolution would make any re-dispatch of a ticket carrying one emit
    # `resolution_consumed: true` forever, defeating the anti-gaming ceiling
    # `cw.dispatch.productivity` relies on this field for.
    resolution_consumed: StrictBool = False
    # Provenance for resolution_consumed above: the settlement round's source
    # comment id/URL and the settled item ids. None when resolution_consumed
    # is False or absent. See GitHub issue #1896.
    resolution_evidence: dict[str, Any] | None = None
    # v8: SHA-256 (full hex) of `.cw/plan-draft.md` with its bookkeeping lines
    # stripped, per auto-dev-plan.md's *Plan-draft fingerprint rule* (#2102).
    # Emitted at every plan-stage sentinel that has a draft in hand; null when
    # no draft exists. `cw dev-queue approve` copies it onto the row as
    # `plan_approved_fingerprint`, which is what lets the next round's
    # Checkpoint 1 tell "approved, and the text is unchanged" from "approved,
    # but this is a different draft now".
    plan_draft_fingerprint: str | None = None

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
