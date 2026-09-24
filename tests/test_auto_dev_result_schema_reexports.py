"""Re-export completeness guard for the ``cw.auto_dev_result.schema`` package (#2193).

The flat ``schema.py`` -> ``cw/auto_dev_result/schema/`` package split must
preserve every ``from cw.auto_dev_result.schema import X`` call site unchanged —
most importantly the 48-name re-export block in
``cw/auto_dev_result/__init__.py``, which is the surface every
``from cw.auto_dev_result import X`` site across ``src/`` and ``tests/`` reaches
through. This mirrors ``tests/test_review_findings_reexports.py``'s
``TestPackageExportCompleteness`` (written for the #1818 split): ``__all__`` is
asserted against an exhaustive hardcoded set, deliberately NOT re-derived from
the package, so a dropped or renamed export is a falsifiable failure rather than
a tautology. A deliberate addition updates this set in the same commit.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from cw import auto_dev_result
from cw.auto_dev_result import schema
from cw.auto_dev_result.schema import AutoDevResult, Blocker

# The complete re-export surface: every top-level name the flat ``schema.py``
# bound before the split. 48 of these are re-exported one level out by
# ``cw.auto_dev_result.__init__``; ``SchemaVersion`` is deliberately excluded
# from that outer block (``parse.py`` imports it from here directly).
EXPECTED_EXPORTS = {
    # Vocabulary — type aliases (schema/_vocab.py)
    "PlanSource",
    "SchemaVersion",
    "ScopeTier",
    "StageReached",
    "Status",
    # Vocabulary — public constants
    "DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON",
    "EMPTY_DIFF_BLOCKER_REASON",
    "EXTERNAL_STATE_BLOCKER_REASON",
    "FINALIZE_REGRESS_BLOCKER_REASONS",
    "FINALIZE_REGRESS_CAP",
    "FREEFORM_BLOCKER_REASON_PREFIX",
    "INTERMEDIATE_ADVANCE_STATUSES",
    "KNOWN_BLOCKER_REASONS",
    "OPERATOR_UNAVAILABLE_BLOCKER_REASONS",
    "PAUSED_FOR_USER_INPUT_STATUSES",
    "PLAN_SOURCE_NONE",
    "SALVAGE_HOLD_STATUSES",
    "SALVAGE_TERMINAL_STATUSES",
    "SCOPE_GATED_APPROVAL_STATUSES",
    "SCOPE_TIER_LARGE",
    "SCOPE_TIER_SMALL",
    "STAGE_FAILURE_STATUSES",
    "STAGE_SUCCESS_STATUSES",
    "STALE_DISPATCH_BLOCKER_REASON",
    # Vocabulary — private constants with confirmed cross-module import sites
    "_MIN_V2_SCHEMA_VERSION",
    "_STAGE_NUMBER_FALLBACK",
    "_STAGE_REACHED_ALIASES",
    "_STAGE_REACHED_CANONICAL",
    "_V2_STATUSES",
    "_V4_STATUSES",
    # Vocabulary — public functions
    "is_known_blocker_reason",
    "queue_status_for_terminal_sentinel",
    # Shared validator helpers (schema/_validators.py)
    "_has_usable_premise_text",
    "_has_usable_question",
    "_is_blank",
    "_is_resolved_premise",
    "_reject_empty_string_items",
    # Nested sub-models (schema/_models.py)
    "AgentHealthEntry",
    "Blocker",
    "Health",
    "PrCreated",
    "PrInfo",
    "Review",
    "Scope",
    # Sentinel result models + their private constants (schema/_result.py)
    "AutoDevResult",
    "BlockedResult",
    "USER_DIRECTED_PREFIXES",
    "_PRE_BRANCH_STATUSES",
    "_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS",
    "_TERMINAL_REJECT_STATUSES",
}


class TestPackageExportCompleteness:
    """Guards that ``cw.auto_dev_result.schema`` re-exports its full surface."""

    def test_all_matches_full_surface(self) -> None:
        assert set(schema.__all__) == EXPECTED_EXPORTS

    def test_every_exported_name_is_bound(self) -> None:
        """A typo'd re-export must fail here, not at a downstream import site."""
        missing = [name for name in EXPECTED_EXPORTS if not hasattr(schema, name)]
        assert missing == []


# ``SchemaVersion`` is deliberately excluded from the outer package's
# re-export block (``cw/auto_dev_result/__init__.py``) — ``parse.py`` imports
# it from ``cw.auto_dev_result.schema`` directly. Every other name in
# ``EXPECTED_EXPORTS`` must still reach the outer ``cw.auto_dev_result``
# surface, since every ``from cw.auto_dev_result import X`` call site across
# ``src/`` and ``tests/`` resolves through that outer block, not this inner one.
EXPECTED_OUTER_SCHEMA_EXPORTS = EXPECTED_EXPORTS - {"SchemaVersion"}


class TestOuterPackageExportCompleteness:
    """Guards that ``cw.auto_dev_result``'s own re-export block stays complete.

    Mirrors ``TestPackageExportCompleteness`` above but targets the outer,
    pre-existing ``cw/auto_dev_result/__init__.py`` — the surface named
    explicitly in the operator's binding plan-resolution comment on #2193.
    """

    def test_outer_all_includes_full_schema_surface(self) -> None:
        assert set(auto_dev_result.__all__) >= EXPECTED_OUTER_SCHEMA_EXPORTS

    def test_every_outer_exported_schema_name_is_bound(self) -> None:
        """A typo'd or dropped re-export must fail here, not downstream."""
        missing = [
            name
            for name in EXPECTED_OUTER_SCHEMA_EXPORTS
            if not hasattr(auto_dev_result, name)
        ]
        assert missing == []


# Every record the schema package emits must carry the pre-split logger name
# ``cw.auto_dev_result`` verbatim. ``caplog.at_level(..., logger=...)`` alone
# cannot catch a rename — level inheritance and propagation make a
# ``__name__``-derived child logger (``cw.auto_dev_result.schema._models``) pass
# the same assertions — so these tests pin ``record.name`` exactly.
PINNED_LOGGER_NAME = "cw.auto_dev_result"


class TestLoggerNamePinned:
    """Guards that the package split did not rename the emitted logger (#2193)."""

    def test_models_warning_uses_pinned_logger_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``Blocker`` lives in ``schema/_models.py``."""
        with caplog.at_level(logging.WARNING, logger=PINNED_LOGGER_NAME):
            Blocker(stage="stage2_impl", reason="not_a_registered_reason")
        records = [r for r in caplog.records if "blocker_reason_unknown" in r.message]
        assert [r.name for r in records] == [PINNED_LOGGER_NAME]

    def test_result_warning_uses_pinned_logger_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``AutoDevResult`` lives in ``schema/_result.py``.

        The near-miss ``stage_reached`` coercion warns from a ``mode="before"``
        field validator, so the record is emitted even though the deliberately
        minimal payload then fails validation on its other required fields.
        """
        with (
            caplog.at_level(logging.WARNING, logger=PINNED_LOGGER_NAME),
            pytest.raises(ValidationError),
        ):
            AutoDevResult.model_validate({"stage_reached": "stage4_pr_creation"})
        records = [r for r in caplog.records if "not canonical" in r.message]
        assert [r.name for r in records] == [PINNED_LOGGER_NAME]
