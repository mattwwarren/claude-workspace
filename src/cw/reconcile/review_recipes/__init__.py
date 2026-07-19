"""Daemon-side review recipes: address-review candidate detection (RFC 0010).

Package split (#1315, part 1 of 2). The historical flat ``cw.reconcile.review_recipes``
module is now a package: ``_shared`` (cross-recipe detect/act infra),
``address_review`` (the P1/P2 address_review recipe), and ``_remaining`` (the
auto_fix_ci / request_reviewer / escalate_merge_block recipes plus the
``run_review_recipes`` tick entry point and ``_detect_repeat_fire_counts`` —
the leftover targeted by part 2). This ``__init__`` re-exports the full
historical public + private surface so every
``from cw.reconcile.review_recipes import X`` import site and downstream call
path keeps working unchanged.
"""

from __future__ import annotations

from cw.reconcile.review_recipes._remaining import (
    _act_auto_fix_ci,
    _act_escalate_merge_block,
    _act_request_reviewer,
    _detect_auto_fix_ci,
    _detect_escalate_merge_block,
    _detect_repeat_fire_counts,
    _detect_request_reviewer,
    run_review_recipes,
)
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
