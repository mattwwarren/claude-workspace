"""Tests for cw.reconcile.gate_predicates — which pending gates need a person.

The detect/act behavior built on these predicates is covered end to end in
``test_reconcile_gate_recipes.py``; this module pins each predicate directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cw.models import Stage
from cw.reconcile.gate_predicates import (
    _clean_review_snapshot,
    _plan_gate_snapshot,
    _plan_predicate_holds,
    _predicate_holds,
    _row_eligible,
)
from tests.conftest import _make_ticket_task

_FP = "c" * 64
_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def _plan_result(**scope: object) -> dict[str, object]:
    return {
        "status": "plan_pending_approval",
        "plan_draft_fingerprint": _FP,
        "scope": {"tier": "large", "forbidden_touched": False, **scope},
    }


class TestRowEligible:
    @pytest.mark.parametrize(
        ("row_kwargs", "stage", "expected"),
        [
            ({"stage": Stage.PLAN}, Stage.PLAN, True),
            ({"stage": Stage.REVIEW}, Stage.REVIEW, True),
            ({"stage": Stage.REVIEW, "scope_hint": "small"}, Stage.REVIEW, True),
            ({"stage": Stage.REVIEW}, Stage.PLAN, False),
            ({"stage": Stage.PLAN, "scope_hint": "large"}, Stage.PLAN, False),
        ],
    )
    def test_stage_and_operator_scope_hint(
        self, row_kwargs: dict[str, Any], stage: Stage, expected: bool
    ) -> None:
        assert _row_eligible(_make_ticket_task(**row_kwargs), stage) is expected


class TestReviewPredicate:
    def test_fixed_and_deferred_findings_do_not_block(self) -> None:
        snapshot = _clean_review_snapshot(
            {
                "status": "review_pending_approval",
                "review": {"must_fix_initial": 4, "deferred": 2, "agents_run": 3},
                "health": {"recommendation": "PROCEED"},
                "scope": {"forbidden_touched": False},
            }
        )
        assert snapshot is not None
        assert snapshot["must_fix_initial"] == 4
        assert snapshot["deferred"] == 2
        assert _predicate_holds(snapshot) is True


class TestPlanGateSnapshot:
    def test_extracts_scope_and_fingerprint(self) -> None:
        assert _plan_gate_snapshot(_plan_result(files=3, lines_estimate=726)) == {
            "tier": "large",
            "files": 3,
            "lines_estimate": 726,
            "forbidden_touched": False,
            "plan_draft_fingerprint": _FP,
        }

    @pytest.mark.parametrize(
        "last_result",
        [
            None,
            "not-a-dict",
            {"status": "review_pending_approval", "scope": {}},
            {"status": "plan_pending_approval"},
            {"status": "plan_pending_approval", "scope": "not-a-dict"},
        ],
    )
    def test_not_fireable_yields_none(self, last_result: object) -> None:
        assert _plan_gate_snapshot(last_result) is None


class TestPlanPredicate:
    def _snapshot(self, **overrides: object) -> dict[str, object]:
        snapshot = _plan_gate_snapshot(_plan_result())
        assert snapshot is not None
        return {**snapshot, **overrides}

    def test_unforbidden_bound_unapproved_draft_holds(self) -> None:
        assert _plan_predicate_holds(self._snapshot(), _make_ticket_task()) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"forbidden_touched": True},
            {"forbidden_touched": None},
            {"plan_draft_fingerprint": None},
            {"plan_draft_fingerprint": "A" * 64},
            {"plan_draft_fingerprint": 12345},
        ],
    )
    def test_forbidden_or_unbound_draft_fails(self, overrides: dict[str, Any]) -> None:
        assert (
            _plan_predicate_holds(self._snapshot(**overrides), _make_ticket_task())
            is False
        )

    def test_already_approved_draft_fails_as_loop_guard(self) -> None:
        task = _make_ticket_task(plan_approved_at=_NOW, plan_approved_fingerprint=_FP)
        assert _plan_predicate_holds(self._snapshot(), task) is False

    def test_fingerprint_without_approval_timestamp_does_not_block(self) -> None:
        """Only an approval that actually happened (timestamp set) is a loop."""
        task = _make_ticket_task(plan_approved_fingerprint=_FP)
        assert _plan_predicate_holds(self._snapshot(), task) is True
