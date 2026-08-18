"""Effective scope-tier resolution and carried-context persistence.

Extracted from the flat ``dispatch/routing.py`` by #1728. Every function here
reads a raw sentinel dict (``session.last_result``) and answers one question:
*what scope tier does this ticket actually route at, and what of the sentinel's
context must survive onto the task row?* No routing decision is made here — the
Rule 1-6 table in ``dispatch/routing/__init__.py`` owns that, and
``review_gates._should_gate_for_scope_hint`` reaches ``_resolve_scope_tier``
through the package facade.

No back-dependency on ``routing/__init__.py``: nothing in this module calls
``record_event``, ``_stage_regress``, or ``_stage_advance_unchecked``, which is
precisely why it was safe to move out (see the package ``__init__``'s
"Monkeypatch coupling" note).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    PLAN_SOURCE_NONE,
    SCOPE_TIER_LARGE,
)

if TYPE_CHECKING:
    from cw.models import TicketTask


def _extract_scope_tier(last_result: dict[str, object] | None) -> str | None:
    """Pull ``scope.tier`` off a raw sentinel dict, tolerating a missing/non-dict
    ``scope`` key. Shared by ``_persist_carried_context`` and
    ``_resolve_scope_tier`` so the two never drift on how they read the field.
    """
    scope_val = last_result.get("scope") if last_result is not None else None
    return scope_val.get("tier") if isinstance(scope_val, dict) else None


def _persist_carried_context(
    task: TicketTask, last_result: dict[str, object] | None
) -> None:
    """Stamp carried-through context (plan_source, computed scope tier) onto the
    task from a stage-matched sentinel, so a rescue respawn's fresh claim->spawn
    re-materializes it via cw-context.json (#1050). Null/pre-impl values and a
    stray plan_source=PLAN_SOURCE_NONE never clobber an already-set value.
    """
    if not isinstance(last_result, dict):
        return
    plan_source = last_result.get("plan_source")
    if isinstance(plan_source, str) and plan_source not in ("", PLAN_SOURCE_NONE):
        task.plan_source = plan_source
    tier = _extract_scope_tier(last_result)
    if isinstance(tier, str) and tier:
        task.computed_scope_tier = tier


def _resolve_scope_tier(
    last_result: dict[str, object] | None, task: TicketTask
) -> str | None:
    """Resolve the effective scope tier for a scope-gated advance decision.

    Precedence (escalate-only, #314, #696, #926):
      0. If either ``task.scope_hint`` or the sentinel's ``scope.tier`` is
         ``"large"``, the result is ``"large"`` -- an operator hint can only
         ADD the approval gate, never remove it, and a large sentinel tier is
         never de-escalated by a smaller hint.
      1. Otherwise, ``last_result.scope.tier`` -- the plan sentinel's own
         classification.
      2. Otherwise, ``task.scope_hint`` -- operator/queue hint, used when the
         sentinel omits the tier.

    Why: a real PLAN-stage sentinel can legitimately carry ``scope.tier=null``
    (``lines_actual`` is unknown pre-impl), so a raw read blocked small tickets
    that should flow PLAN->IMPL unattended (#663 dogfood). Returns ``None`` when
    neither source supplies a tier -- the caller then blocks conservatively.
    """
    tier = _extract_scope_tier(last_result)
    # Step 0 of the precedence above. Only the exact string "large" escalates;
    # unexpected hint values (e.g. "medium") are treated as not-large.
    if SCOPE_TIER_LARGE in (task.scope_hint, tier):
        return SCOPE_TIER_LARGE
    if isinstance(tier, str):
        return tier
    return task.scope_hint
