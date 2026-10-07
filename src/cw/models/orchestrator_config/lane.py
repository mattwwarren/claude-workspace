"""Named dispatch-lane configuration: ``LaneConfig``.

Also holds ``CODEX_TIER_CLAIM_SUPPRESSION`` and the codex-review-tier key
validator. Depends on ``cw.models.orchestrator_config.stage``,
``cw.models.tasks`` (for the shared recipe-key validators) and
``cw.models.enums``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cw.models.enums import ReapPolicy
from cw.models.orchestrator_config.stage import StagePipelineConfig
from cw.models.tasks import (
    _validate_gate_recipe_keys,
    _validate_park_on_abandoned_exit_keys,
    _validate_review_recipe_keys,
)

#: The one recognised key of :attr:`LaneConfig.codex_review_tiers` (#2210).
#: A named constant, not a bare literal, because ``cw.codex_background``'s
#: resolver and its hardcoded-off floor key on the same string.
CODEX_TIER_CLAIM_SUPPRESSION = "claim_suppression"
_CODEX_REVIEW_TIER_KEYS = frozenset({CODEX_TIER_CLAIM_SUPPRESSION})


def _validate_codex_review_tier_keys(value: dict[str, bool]) -> dict[str, bool]:
    """Fail loud on an unrecognized codex-review-tier key (#2210).

    Same stance, and the same reason, as ``_validate_gate_recipe_keys``: a
    typo'd key would otherwise resolve silently to the hardcoded default-off,
    leaving the operator convinced they armed a tier they did not.
    """
    unknown = sorted(set(value) - _CODEX_REVIEW_TIER_KEYS)
    if unknown:
        msg = (
            f"codex_review_tiers has unrecognized tier key(s): {unknown}. "
            f"Recognised keys: {sorted(_CODEX_REVIEW_TIER_KEYS)}."
        )
        raise ValueError(msg)
    return value


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
    # Lane-level overrides for the `cw guard-busy-wait` PreToolUse guard
    # (#1946). Shaped on reap_policy and codex_fix_loop_enabled below, not on
    # the opt-in-only Literal[True] fields (signoff, finalize_gate): a lane must
    # be able to turn the guard OFF against an enabled global (a lane whose
    # workers legitimately poll) and ON against a disabled one, so the override
    # is bidirectional. None on any
    # of the three = inherit the OrchestratorConfig default. Resolved by
    # cw.cli.guard_busy_wait._resolve_settings, which mirrors
    # resolve_reap_policy's lane-then-global fallthrough rather than importing
    # it (that function's signature is reconcile-specific -- it takes a
    # ReapCandidate, which no hook subprocess ever has).
    busy_wait_guard_enabled: bool | None = None
    busy_wait_guard_repeat_threshold: int | None = Field(default=None, ge=2)
    busy_wait_guard_window_seconds: int | None = Field(default=None, ge=1)
    # Lane-level override for the disposition ledger's drift check (#2232).
    # Same bidirectional shape and reasoning as busy_wait_guard_enabled above:
    # None = inherit the OrchestratorConfig default. Resolved by
    # cw.codex_background._resolve_disposition_drift_check_enabled, which
    # mirrors guard_busy_wait._resolve_settings' lane-then-global fallthrough
    # rather than _resolve_claim_tier_enabled's master-switch-then-floor shape
    # -- there is no kill switch and no floor for a check that defaults on.
    # Turning it off for a lane REFUSES to arm that lane's claim tier; see
    # cw.exceptions.ClaimTierArmingError.
    disposition_drift_check_enabled: bool | None = None
    # Lane-level override for the `cw agent-spawn-pre` spawn-shape policy
    # (#2211). Same bidirectional shape and reasoning as
    # busy_wait_guard_enabled above: None = inherit the OrchestratorConfig
    # default. Resolved by cw.cli._hook_io.resolve_guard_enabled (a
    # GuardToggle: renaming this field means renaming it there too).
    subagent_spawn_guard_enabled: bool | None = None
    # Lane-level override for the `cw background-tool-guard-pre` guard
    # (#2303). Same bidirectional shape and reasoning as
    # subagent_spawn_guard_enabled above: None = inherit the
    # OrchestratorConfig default. Resolved by
    # cw.cli._hook_io.resolve_guard_enabled (a GuardToggle, likewise).
    background_tool_guard_enabled: bool | None = None
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
    # resolve_reap_policy's lane-then-global fallthrough shape. A plain bool
    # override (#2541): True opts the lane IN, False opts the lane OUT against a
    # globally enabled default_codex_fix_loop_enabled (no workspace-write fix
    # pass ever runs on that lane), and None defers to the global.
    codex_fix_loop_enabled: bool | None = None
    # Lane-level override for the global attempt ceiling (#1751, scoping the
    # flat #786 bound that #1750 re-pointed at unproductive_attempts).
    # Precedence: lane > OrchestratorConfig.global_attempt_ceiling. Resolved by
    # cw.reconcile._shared.resolve_attempt_ceiling, which BOTH the dispatch
    # claim path and the concierge recovery recipes call -- they must agree on
    # the number or the concierge would refuse a requeue the claim path would
    # have allowed (the drift #1750's own comments warn against).
    #
    # Tri-state, and NOT a plain `bool | None` override like
    # codex_fix_loop_enabled above: the global here is a number, not an on/off
    # flag, so the lane needs a distinct "disable" token. `None` = the lane
    # sets no override (defer to global) -- the meaning every sibling already
    # assigns to None, which is exactly why `False`, not `None`, is the
    # disable token here: a lane that wants to
    # inherit whatever the global ceiling later becomes and a lane that wants
    # no ceiling ever are different intents that must stay distinguishable, and
    # Pydantic collapses "key absent" and "key present: null" to the same None.
    # `False` = the lane explicitly disables the ceiling (a supervised lane
    # whose operator answers every park IS the rate limiter, so an automated
    # bound buys nothing). A positive int = the lane's own ceiling.
    attempt_ceiling: Literal[False] | int | None = None
    # Lane-level gate-recipe enablement map (RFC 0009 P4, #1067). Middle tier in
    # resolve_gate_recipe_enabled's 3-tier precedence: consulted when the ticket
    # carries no override for the recipe, and itself overridden by
    # TicketTask.gate_recipes. A recipe absent from this map (or None) defers to
    # the hardcoded floor (on). Recognised keys: "auto_approve_clean_review",
    # "auto_adopt_clean_plan".
    gate_recipes: dict[str, bool] | None = None
    # Lane-level review-recipe enablement map (RFC 0010 P3, #1098). Middle tier
    # in resolve_review_recipe_enabled's 3-tier precedence: consulted when the
    # ticket carries no override for the recipe, and itself overridden by
    # TicketTask.review_recipes. A recipe absent from this map (or None) defers
    # to the hardcoded default-off. Recognised keys: "address_review",
    # "auto_fix_ci", "request_reviewer", "escalate_merge_block" (RFC 0010 P4).
    review_recipes: dict[str, bool] | None = None
    # Lane-level enablement map for the Stop-hook abandoned-exit park (#2135).
    # Middle tier in resolve_park_on_abandoned_exit_enabled's 3-tier
    # precedence: consulted when the ticket carries no override, and itself
    # overridden by TicketTask.park_on_abandoned_exit. The key absent from this
    # map (or None) defers to the hardcoded default-off. Recognised key:
    # PARK_ON_ABANDONED_EXIT_KEY.
    park_on_abandoned_exit: dict[str, bool] | None = None
    # Lane-level codex-review tier enablement map (#2210). Middle tier in
    # cw.codex_background._resolve_claim_tier_enabled's precedence, which is
    # 2-tier rather than 3 (master switch -> lane map -> hardcoded-off floor):
    # a per-ticket override would be a persisted dev-queue schema change this
    # ticket deliberately does not make. A tier absent from this map (or None)
    # defers to the hardcoded default-off. Recognised keys:
    # "claim_suppression". See ADR-0016.
    codex_review_tiers: dict[str, bool] | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v:
            msg = "lane name must be non-empty"
            raise ValueError(msg)
        return v

    @field_validator("attempt_ceiling", mode="before")
    @classmethod
    def _check_attempt_ceiling(cls, value: object) -> object:
        """Reject the two raw values Pydantic's smart union silently reinterprets.

        ``bool`` is an ``int`` subclass, so ``Literal[False] | int`` resolves
        ``0`` to ``False`` (i.e. "disabled" -- the *opposite* of what an
        operator writing "cap at 0" means) and ``True`` to ``1`` (a ceiling of
        one unproductive attempt, not "enabled"). Both are plausible typos in
        hand-edited YAML, so they fail loudly here rather than being quietly
        reinterpreted. Runs ``mode="before"`` because by the time the union has
        run, the evidence of which literal was written is already gone.
        """
        if isinstance(value, bool):
            if value is True:
                msg = (
                    "lane attempt_ceiling does not accept true; use a positive"
                    " integer to set a lane ceiling, false to disable the"
                    " ceiling, or omit the key to defer to"
                    " global_attempt_ceiling"
                )
                raise ValueError(msg)
            return value
        if isinstance(value, int) and value <= 0:
            msg = (
                f"lane attempt_ceiling must be a positive integer (got {value});"
                " use false to disable the ceiling, or omit the key to defer to"
                " global_attempt_ceiling"
            )
            raise ValueError(msg)
        return value

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

    @field_validator("codex_review_tiers")
    @classmethod
    def _check_codex_review_tiers(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return None
        return _validate_codex_review_tier_keys(value)
