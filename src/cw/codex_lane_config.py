"""Codex review-lane configuration resolvers (#3054, moved from ``codex_background``).

Pure resolvers: each takes the client, task and loaded orchestrator config and
returns the effective setting for the task's lane, most-specific-wins. Leaf
module: it imports only ``cw.models``, ``cw.exceptions`` and the type-only
config names, so reconcile and doctor can read the gates without importing the
heavy ``cw.codex_background`` thread module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NamedTuple

from cw.exceptions import ClaimTierArmingError
from cw.models.orchestrator_config import CODEX_TIER_CLAIM_SUPPRESSION

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig, TicketTask

__all__ = [
    "_CLAIM_TIER_ARMING_REFUSAL",
    "_DEFAULT_CODEX_REVIEW_TIER_ENABLED",
    "CodexFixLoopResolution",
    "CodexFixLoopSource",
    "_refuse_unguarded_claim_tier",
    "_resolve_claim_tier_enabled",
    "_resolve_codex_fix_loop",
    "_resolve_codex_fix_loop_enabled",
    "_resolve_codex_fix_loop_growth_guard_enabled",
    "_resolve_codex_fix_loop_stall_cycles",
    "_resolve_disposition_drift_check_enabled",
]


CodexFixLoopSource = Literal["lane", "global default"]


class CodexFixLoopResolution(NamedTuple):
    """The resolved fix-loop gate and which precedence level decided it (#2542)."""

    enabled: bool
    source: CodexFixLoopSource


def _resolve_codex_fix_loop(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> CodexFixLoopResolution:
    """Resolve the effective codex_fix_loop_enabled gate for *task* (#1553).

    Precedence (highest to lowest):
      1. Lane-level LaneConfig.codex_fix_loop_enabled in *client*'s config.
         An explicit ``False`` wins over a global ``True`` (lane opt-out,
         #2541); ``None`` falls through.
      2. Global OrchestratorConfig.default_codex_fix_loop_enabled.

    A task whose lane name is not declared in the client's lanes falls
    through to the global default. Mirrors resolve_reap_policy's lane-then-
    global fallthrough shape (cw.reconcile._shared). The ``source`` names the
    winning level; ``cw doctor`` reads it (#2542).
    """
    for lane_cfg in client.effective_lanes:
        if lane_cfg.name == task.lane and lane_cfg.codex_fix_loop_enabled is not None:
            return CodexFixLoopResolution(lane_cfg.codex_fix_loop_enabled, "lane")
    return CodexFixLoopResolution(
        config.default_codex_fix_loop_enabled, "global default"
    )


def _resolve_codex_fix_loop_enabled(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> bool:
    """Bool gate for runtime callers; precedence lives in _resolve_codex_fix_loop."""
    return _resolve_codex_fix_loop(client, task, config).enabled


#: The hardcoded-off floor for the codex review tiers (#2210), the shape of
#: ``reconcile.gate_recipes._DEFAULT_GATE_RECIPE_ENABLED`` but off. A lane that names
#: no tier lands here, so a tier can never arm by accident or by omission.
_DEFAULT_CODEX_REVIEW_TIER_ENABLED: dict[str, bool] = {
    CODEX_TIER_CLAIM_SUPPRESSION: False,
}


def _resolve_claim_tier_enabled(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> bool:
    """Resolve the claim-match suppression tier for *task* (#2210, ADR-0016).

    Two switches, both of which must be true, resolved most-specific-wins down
    to a hardcoded-off floor — the default-off flag convention
    ``docs/release-playbook.md`` records, minus its optional ``TicketTask``
    tier (that would be a persisted dev-queue schema change).

      1. ``OrchestratorConfig.codex_claim_suppression_enabled`` (master).
         False here is a kill switch no lane can override.
      2. The task's lane's ``LaneConfig.codex_review_tiers``.
      3. Otherwise :data:`_DEFAULT_CODEX_REVIEW_TIER_ENABLED` (False).

    Resolved here, beside :func:`_resolve_codex_fix_loop_enabled`, because this
    is the one place where the lane, the client's lanes and the loaded
    ``OrchestratorConfig`` are all already in hand — no new config I/O. A
    client with no declared lanes gets a synthesised ``default`` lane carrying
    no tier map, so arming a lane means declaring it first.
    """
    if not config.codex_claim_suppression_enabled:
        return False
    for lane_cfg in client.effective_lanes:
        if lane_cfg.name != task.lane or lane_cfg.codex_review_tiers is None:
            continue
        enabled = lane_cfg.codex_review_tiers.get(CODEX_TIER_CLAIM_SUPPRESSION)
        if enabled is not None:
            return enabled
    return _DEFAULT_CODEX_REVIEW_TIER_ENABLED[CODEX_TIER_CLAIM_SUPPRESSION]


def _resolve_disposition_drift_check_enabled(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> bool:
    """Resolve the ledger's drift check for *task* (#2232, ADR-0016).

    Two levels, not three: a non-``None``
    ``LaneConfig.disposition_drift_check_enabled`` for the task's lane wins,
    otherwise ``OrchestratorConfig.disposition_drift_check_enabled``, which
    defaults ``True``. Shaped on
    ``cw.cli.guard_busy_wait._resolve_settings``' lane-then-global
    fallthrough rather than on :func:`_resolve_claim_tier_enabled`'s
    master-switch-then-lane-then-floor shape, because there is neither a
    separate kill switch nor a hardcoded floor for a check that defaults on —
    a floor would be the opposite of fail-safe here.

    Resolved beside the claim tier, from the same three already-loaded
    objects, so the two are in hand together at the one point a refusal can
    compare them.
    """
    for lane_cfg in client.effective_lanes:
        if lane_cfg.name != task.lane:
            continue
        if lane_cfg.disposition_drift_check_enabled is not None:
            return lane_cfg.disposition_drift_check_enabled
    return config.disposition_drift_check_enabled


def _resolve_codex_fix_loop_stall_cycles(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> int:
    """Resolve the fix loop's divergence stall count for *task* (#2633).

    A non-``None`` ``LaneConfig.codex_fix_loop_stall_cycles`` for the task's
    lane wins, otherwise ``OrchestratorConfig.codex_fix_loop_stall_cycles``
    (default 1). Same lane-then-global shape as
    :func:`_resolve_disposition_drift_check_enabled`.
    """
    for lane_cfg in client.effective_lanes:
        lane_cycles = lane_cfg.codex_fix_loop_stall_cycles
        if lane_cfg.name == task.lane and lane_cycles is not None:
            return lane_cycles
    return config.codex_fix_loop_stall_cycles


def _resolve_codex_fix_loop_growth_guard_enabled(
    client: ClientConfig, task: TicketTask, config: OrchestratorConfig
) -> bool:
    """Resolve the fix loop's heuristic growth-guard switch for *task* (#2633).

    A non-``None`` ``LaneConfig.codex_fix_loop_growth_guard_enabled`` for the
    task's lane wins, otherwise
    ``OrchestratorConfig.codex_fix_loop_growth_guard_enabled`` (default
    ``True``). Same lane-then-global shape as
    :func:`_resolve_disposition_drift_check_enabled`.
    """
    for lane_cfg in client.effective_lanes:
        lane_enabled = lane_cfg.codex_fix_loop_growth_guard_enabled
        if lane_cfg.name == task.lane and lane_enabled is not None:
            return lane_enabled
    return config.codex_fix_loop_growth_guard_enabled


#: The arming refusal's message. Spelled once, here, because it is the ONLY
#: operator-facing surface the refusal has (it reaches them through
#: ``_log.exception``'s traceback, not a dedicated blocked reason), and it has
#: to name both settings and the fix rather than just the symptom.
_CLAIM_TIER_ARMING_REFUSAL = (
    "Cannot run the codex review ledger's claim-match suppression tier for "
    "lane {lane!r}: disposition_drift_check_enabled resolves to False, but "
    "the claim tier (codex_claim_suppression_enabled / "
    "codex_review_tiers['claim_suppression']) resolves to True for this task. "
    "Drift-checking is what keeps a settle whose code has moved from silently "
    "suppressing a re-raised finding, which is the protection the fuzzy tier "
    "depends on (ADR-0016, #2232). Enable disposition_drift_check_enabled — "
    "globally in orchestrator.yaml, or for this lane in clients.yaml — before "
    "arming the claim tier, or disable the claim tier until it is."
)


def _refuse_unguarded_claim_tier(
    *, lane: str, claim_tier_enabled: bool, drift_check_enabled: bool
) -> None:
    """Refuse to arm the claim tier while the drift check is off (#2232).

    Exactly one cell of the 2x2 refuses; the other three proceed untouched.
    Deliberately not a general cross-field config validator — the operator
    scoped this to the single precondition ADR-0016 names.
    """
    if claim_tier_enabled and not drift_check_enabled:
        raise ClaimTierArmingError(_CLAIM_TIER_ARMING_REFUSAL.format(lane=lane))
