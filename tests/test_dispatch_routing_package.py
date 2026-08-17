"""Seam assertions for the ``cw.dispatch.routing`` package split (#1728).

``routing`` was a single 1345-line flat module. It is now a package whose
``__init__`` retains the Rule 1-6 decision table and re-exports four
concern-scoped submodules. Two things have to stay true forever, and neither is
covered by the behavioral suite:

1. The four submodules genuinely own their concern (a silent re-merge back into
   ``__init__`` would leave every behavioral test green).
2. The decision-table core stays *defined in* the module object bound to
   ``cw.dispatch.routing``. ``tests/test_dispatch.py`` monkeypatches
   ``cw.dispatch.routing.record_event`` / ``._stage_regress`` /
   ``._stage_advance_unchecked`` by dotted path at 10 sites, and a function
   resolves its globals against the module it was *defined* in (see
   ``tests/conftest.py``'s ``capture_events`` docstring). Moving any of those
   functions into a submodule would silently decouple the patches from the real
   call sites — the tests would still pass their setattr and then observe
   nothing.
"""

from __future__ import annotations

_ROUTING_MODULE = "cw.dispatch.routing"

# The full historical surface ``cw.dispatch.__init__`` re-exports from
# ``routing`` (dispatch/__init__.py's ``from cw.dispatch.routing import (...)``
# block). Every name must remain a package-level attribute after the split.
_HISTORICAL_SURFACE = (
    "_APPROVAL_GATE_REASON",
    "_AWAITING_OPERATOR_REASON",
    "_EARLIER_STAGE_REPORT_REASON",
    "_INVALID_STAGE_REASON",
    "_PLAN_PARKED_REASON",
    "_RULE_GATE_RELEASE",
    "_STAGE_REACHED_TO_STAGE",
    "_UNKNOWN_CLIENT_REASON",
    "BREADCRUMB_ELIGIBLE_PAUSED_STATUSES",
    "_accumulate_task_cost",
    "_classify_sentinel_stage_position",
    "_extract_scope_tier",
    "_persist_carried_context",
    "_record_scope_routing_decision",
    "_resolve_scope_tier",
    "_resolve_stage_walk",
    "_route_scope_gated_approval",
    "_route_stage_success",
    "_route_staged_decision",
    "_stage_advance_unchecked",
    "_StagePosition",
    "_walk_stage_pointer_forward",
    "apply_staged_decision",
)

# Functions that must stay *defined in* the routing package's ``__init__`` for
# test_dispatch.py's dotted-path monkeypatches to reach the real call sites.
_MONKEYPATCH_COUPLED = (
    "_route_staged_decision",
    "_stage_advance_unchecked",
    "_record_scope_routing_decision",
    "_park_must_fix_mechanically_rejected",
    "_route_scope_gated_approval",
    "_route_stage_success",
    "apply_staged_decision",
)


def test_stage_walk_submodule_owns_the_walk() -> None:
    """Stage classification and the forward pointer walk live in ``stage_walk``."""
    from cw.dispatch.routing import stage_walk

    assert hasattr(stage_walk, "_resolve_stage_walk")
    assert hasattr(stage_walk, "_walk_stage_pointer_forward")
    assert hasattr(stage_walk, "_classify_sentinel_stage_position")


def test_scope_tier_submodule_owns_tier_resolution() -> None:
    """Scope-tier resolution and carried-context persistence live in ``scope_tier``."""
    from cw.dispatch.routing import scope_tier

    assert hasattr(scope_tier, "_resolve_scope_tier")
    assert hasattr(scope_tier, "_extract_scope_tier")
    assert hasattr(scope_tier, "_persist_carried_context")


def test_pr_refs_submodule_owns_blocked_pr_extraction() -> None:
    """Blocker PR cross-reference extraction lives in ``pr_refs``."""
    from cw.dispatch.routing import pr_refs

    assert hasattr(pr_refs, "_extract_blocked_on_pr")
    assert hasattr(pr_refs, "_AUTOMERGE_NOT_ARMED_REASON")
    assert hasattr(pr_refs, "_PRIOR_PIPELINE_PR_OPEN_REASON")


def test_cost_submodule_owns_accumulate_task_cost() -> None:
    """Per-session cost accumulation lives in ``cost``."""
    from cw.dispatch.routing import cost

    assert hasattr(cost, "_accumulate_task_cost")


def test_routing_package_reexports_full_historical_surface() -> None:
    """Every name ``dispatch/__init__.py`` imports from routing stays reachable."""
    from cw.dispatch import routing

    missing = [name for name in _HISTORICAL_SURFACE if not hasattr(routing, name)]
    assert not missing, f"routing lost re-exports: {missing}"


def test_decision_table_core_stays_in_routing_module_object() -> None:
    """The monkeypatch-coupled Rule 1-6 core stays defined in ``routing`` itself.

    ``__module__`` is the assertion, not ``hasattr``: a re-export from a
    submodule would satisfy the latter while breaking every dotted-path
    monkeypatch in ``tests/test_dispatch.py``.
    """
    from cw.dispatch import routing

    for name in _MONKEYPATCH_COUPLED:
        fn = getattr(routing, name)
        assert fn.__module__ == _ROUTING_MODULE, (
            f"{name} moved to {fn.__module__}; test_dispatch.py monkeypatches"
            f" {_ROUTING_MODULE}.record_event/_stage_regress/"
            "_stage_advance_unchecked and would no longer reach it"
        )
