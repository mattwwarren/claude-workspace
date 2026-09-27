"""Pydantic models and schema vocabulary for the ``<<<AUTO_DEV_RESULT`` sentinel.

The headless ``/auto-dev`` skill emits a sentinel-delimited JSON block as the
final lines of stdout summarizing the pipeline outcome. This package owns the
*schema half* of that contract: the :class:`AutoDevResult` model and its nested
models, plus the status/stage/scope vocabulary and cross-field invariants they
enforce. The *parsing half* (stdout extraction, decode, producer-drift
coercion, :func:`~cw.auto_dev_result.parse.parse_stdout`) lives in
:mod:`cw.auto_dev_result.parse`.

This package was split out of a single ``schema.py`` module (#2193); the public
``from cw.auto_dev_result.schema import X`` surface is preserved here via
re-exports, so the 48-name re-export block in ``cw/auto_dev_result/__init__.py``
and every downstream call site keep working unchanged. Submodules, in dependency
order:

- ``_vocab`` — the ``Literal`` aliases (schema version, status, stage, scope
  tier, plan source), the frozensets derived from them, the blocker-reason
  registry, and the two classifier functions that read it.
- ``_validators`` — validator helpers shared by ``_models`` and ``_result``.
- ``_models`` — the seven nested sub-models (``Scope``, ``PrInfo``,
  ``PrCreated``, ``Review``, ``AgentHealthEntry``, ``Health``, ``Blocker``).
- ``_result`` — ``AutoDevResult``, ``BlockedResult``, and the cross-field
  invariants spanning the nested models.

Spec: ``docs/headless-contract.md`` (§3 framing, §4 enum, §5 health, §6
failure modes). Earlier package split: issue #1321.
"""

from __future__ import annotations

from cw.auto_dev_result.schema._models import (
    AgentHealthEntry,
    Blocker,
    Health,
    PrCreated,
    PrInfo,
    Review,
    Scope,
)
from cw.auto_dev_result.schema._result import (
    _PRE_BRANCH_STATUSES,
    _PRE_FLIGHT_BLOCKED_NEXT_ACTIONS,
    _TERMINAL_REJECT_STATUSES,
    USER_DIRECTED_PREFIXES,
    AutoDevResult,
    BlockedResult,
)
from cw.auto_dev_result.schema._validators import (
    _has_usable_premise_text,
    _has_usable_question,
    _is_blank,
    _is_resolved_premise,
    _reject_empty_string_items,
)
from cw.auto_dev_result.schema._vocab import (
    _MIN_V2_SCHEMA_VERSION,
    _STAGE_NUMBER_FALLBACK,
    _STAGE_REACHED_ALIASES,
    _STAGE_REACHED_CANONICAL,
    _V2_STATUSES,
    _V4_STATUSES,
    DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON,
    EMPTY_DIFF_BLOCKER_REASON,
    EXTERNAL_STATE_BLOCKER_REASON,
    FINALIZE_REGRESS_BLOCKER_REASONS,
    FINALIZE_REGRESS_CAP,
    FREEFORM_BLOCKER_REASON_PREFIX,
    IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON,
    INTERMEDIATE_ADVANCE_STATUSES,
    KNOWN_BLOCKER_REASONS,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    PLAN_SCOPE_DRIFT_BLOCKER_REASON,
    PLAN_SOURCE_NONE,
    SALVAGE_HOLD_STATUSES,
    SALVAGE_TERMINAL_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_LARGE,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    STALE_DISPATCH_BLOCKER_REASON,
    PlanSource,
    SchemaVersion,
    ScopeTier,
    StageReached,
    Status,
    is_known_blocker_reason,
    queue_status_for_terminal_sentinel,
)

__all__ = [
    "DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON",
    "EMPTY_DIFF_BLOCKER_REASON",
    "EXTERNAL_STATE_BLOCKER_REASON",
    "FINALIZE_REGRESS_BLOCKER_REASONS",
    "FINALIZE_REGRESS_CAP",
    "FREEFORM_BLOCKER_REASON_PREFIX",
    "IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON",
    "INTERMEDIATE_ADVANCE_STATUSES",
    "KNOWN_BLOCKER_REASONS",
    "OPERATOR_UNAVAILABLE_BLOCKER_REASONS",
    "PAUSED_FOR_USER_INPUT_STATUSES",
    "PLAN_SCOPE_DRIFT_BLOCKER_REASON",
    "PLAN_SOURCE_NONE",
    "SALVAGE_HOLD_STATUSES",
    "SALVAGE_TERMINAL_STATUSES",
    "SCOPE_GATED_APPROVAL_STATUSES",
    "SCOPE_TIER_LARGE",
    "SCOPE_TIER_SMALL",
    "STAGE_FAILURE_STATUSES",
    "STAGE_SUCCESS_STATUSES",
    "STALE_DISPATCH_BLOCKER_REASON",
    "USER_DIRECTED_PREFIXES",
    "_MIN_V2_SCHEMA_VERSION",
    "_PRE_BRANCH_STATUSES",
    "_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS",
    "_STAGE_NUMBER_FALLBACK",
    "_STAGE_REACHED_ALIASES",
    "_STAGE_REACHED_CANONICAL",
    "_TERMINAL_REJECT_STATUSES",
    "_V2_STATUSES",
    "_V4_STATUSES",
    "AgentHealthEntry",
    "AutoDevResult",
    "BlockedResult",
    "Blocker",
    "Health",
    "PlanSource",
    "PrCreated",
    "PrInfo",
    "Review",
    "SchemaVersion",
    "Scope",
    "ScopeTier",
    "StageReached",
    "Status",
    "_has_usable_premise_text",
    "_has_usable_question",
    "_is_blank",
    "_is_resolved_premise",
    "_reject_empty_string_items",
    "is_known_blocker_reason",
    "queue_status_for_terminal_sentinel",
]
