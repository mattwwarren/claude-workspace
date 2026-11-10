"""Lane-config resolvers moved to ``cw.codex_lane_config`` (#3054).

Behavior is pinned by the unedited ``tests/test_codex_background.py``; these tests
pin the seams: the names resolve to the same objects through the old module path,
and the consumers that used to import the heavy thread module now import the leaf.
"""

from __future__ import annotations

import pytest

from cw import codex_background, codex_lane_config
from cw.reconcile import codex_boot, local

# Names ``codex_background`` still exposes: imported for use there or re-exported
# through its ``__all__``. The message constant and the Literal alias live only in
# the leaf module, so no caller reaches them through the thread module.
MOVED_NAMES = (
    "CodexFixLoopResolution",
    "_DEFAULT_CODEX_REVIEW_TIER_ENABLED",
    "_refuse_unguarded_claim_tier",
    "_resolve_claim_tier_enabled",
    "_resolve_codex_fix_loop",
    "_resolve_codex_fix_loop_enabled",
    "_resolve_codex_fix_loop_growth_guard_enabled",
    "_resolve_codex_fix_loop_stall_cycles",
    "_resolve_disposition_drift_check_enabled",
)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_name_is_same_object_on_both_modules(name: str) -> None:
    assert getattr(codex_background, name) is getattr(codex_lane_config, name)


def test_consumers_import_the_leaf_module() -> None:
    assert (
        codex_boot._resolve_codex_fix_loop_enabled
        is codex_lane_config._resolve_codex_fix_loop_enabled
    )
    assert (
        local._resolve_codex_fix_loop_enabled
        is codex_lane_config._resolve_codex_fix_loop_enabled
    )


def test_leaf_module_is_listed_in_its_own_all() -> None:
    assert "_resolve_codex_fix_loop_enabled" in codex_lane_config.__all__
