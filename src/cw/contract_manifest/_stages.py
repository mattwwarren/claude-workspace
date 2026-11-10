"""The shared stage map: which stage's rules a contract element follows.

A stage is a plain string here, so this module needs no ``cw.models`` import;
``tests/test_contract_manifest.py`` pins :data:`STAGES` to the ``Stage`` enum
values. Moved out of the manifest test (RFC 0018 A3, #3058) so the renderer and
the test share one definition.
"""

from __future__ import annotations

from cw.models.orchestrator_config.constants import CODEX_BACKEND

__all__ = ["ORCH_GOVERNS", "SCOPE_STAGE", "STAGES", "scope_stage"]

#: ``orch`` ids whose lead or first sentence names one stage (R23). Every
#: other ``orch`` id is stage-agnostic: codex follows REVIEW rules and local
#: follows IMPL rules for it.
ORCH_GOVERNS: dict[str, str] = {
    "orch.operator-authority-delta": "plan",
    "orch.fast-path-composition-requires-third-condition": "plan",
    "orch.impl-stage-consumption": "impl",
    "orch.marker-plan-signoff": "plan",
    "orch.clean-mode-dispatch-check": "review",
    "orch.post-impl-scope-conformance-threshold": "impl",
    "orch.absent-owner-record": "impl",
    "orch.plan-draft-fingerprint-in-chained-path": "plan",
    "orch.plan-draft-fingerprint": "plan",
    "orch.clean-mode-review-fields": "review",
    "orch.sentinel-review": "review",
    "orch.sentinel-ambiguities": "plan",
    "orch.sentinel-premises": "plan",
}

STAGES: tuple[str, ...] = ("plan", "impl", "review", "finalize")

#: The stage each non-``orch`` scope belongs to; intake folds into plan.
SCOPE_STAGE: dict[str, str] = {
    "intake": "plan",
    "plan": "plan",
    "impl": "impl",
    "review": "review",
    "finalize": "finalize",
    "prep-pr": "finalize",
    "ship-it": "finalize",
}


def scope_stage(element_id: str, backend: str) -> str:
    """The stage whose rules *element_id* follows on *backend* (R23)."""
    scope = element_id.split(".", 1)[0]
    if scope != "orch":
        return SCOPE_STAGE[scope]
    governed = ORCH_GOVERNS.get(element_id)
    if governed is not None:
        return governed
    return "review" if backend == CODEX_BACKEND else "impl"
