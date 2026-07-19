"""Daemon-side review recipes: PR attention-state detect + act (RFC 0010).

Package split (#1315, complete). The historical flat ``cw.reconcile.review_recipes``
module is now a package of six focused modules:

* ``_shared`` — cross-recipe detect/act infra (the pure
  ``_detect_by_attention_state`` classifier, act-phase helpers, and the
  recipe/attention/payload constants).
* ``address_review`` — the P1/P2 ``address_review`` recipe (changes_requested).
* ``auto_fix_ci`` — the ``auto_fix_ci`` recipe (ci_failing re-dispatch).
* ``request_reviewer`` — the ``request_reviewer`` recipe (no_reviewer gh call).
* ``escalate_merge_block`` — the ``escalate_merge_block`` recipe (merge_blocked
  one-shot escalation).
* ``core`` — the ``run_review_recipes`` per-tick detect->act entry point plus the
  stateless ``_detect_repeat_fire_counts`` burst counter.

This ``__init__`` re-exports the full historical public + private surface so every
``from cw.reconcile.review_recipes import X`` import site and downstream call path
keeps working unchanged.
"""

from __future__ import annotations

from cw.reconcile.review_recipes._shared import (
    _REPEAT_FIRE_ATTENTION_REASON,
    RECIPE_ADDRESS_REVIEW,
    RECIPE_ATTENTION_STATES,
    RECIPE_AUTO_FIX_CI,
    RECIPE_ESCALATE_MERGE_BLOCK,
    RECIPE_FIRED_AT_GETTERS,
    RECIPE_REQUEST_REVIEWER,
    ReviewRecipeCandidate,
    _record_pr_action_taken,
    resolve_outbound_consent_allowed,
    resolve_review_recipe_enabled,
)
from cw.reconcile.review_recipes.address_review import (
    _act_address_review,
    _detect_address_review,
)
from cw.reconcile.review_recipes.auto_fix_ci import (
    _act_auto_fix_ci,
    _detect_auto_fix_ci,
)
from cw.reconcile.review_recipes.core import (
    _detect_repeat_fire_counts,
    run_review_recipes,
)
from cw.reconcile.review_recipes.escalate_merge_block import (
    _act_escalate_merge_block,
    _detect_escalate_merge_block,
)
from cw.reconcile.review_recipes.request_reviewer import (
    _act_request_reviewer,
    _detect_request_reviewer,
)

__all__ = [
    "RECIPE_ADDRESS_REVIEW",
    "RECIPE_ATTENTION_STATES",
    "RECIPE_AUTO_FIX_CI",
    "RECIPE_ESCALATE_MERGE_BLOCK",
    "RECIPE_FIRED_AT_GETTERS",
    "RECIPE_REQUEST_REVIEWER",
    "_REPEAT_FIRE_ATTENTION_REASON",
    "ReviewRecipeCandidate",
    "_act_address_review",
    "_act_auto_fix_ci",
    "_act_escalate_merge_block",
    "_act_request_reviewer",
    "_detect_address_review",
    "_detect_auto_fix_ci",
    "_detect_escalate_merge_block",
    "_detect_repeat_fire_counts",
    "_detect_request_reviewer",
    "_record_pr_action_taken",
    "resolve_outbound_consent_allowed",
    "resolve_review_recipe_enabled",
    "run_review_recipes",
]
