"""Tests for ``cw.review_engine.route``: every row of R14, top-down (#3039)."""

from __future__ import annotations

import itertools
import subprocess
import sys

import pytest

from cw.review_adjudication import Adjudication
from cw.review_engine import (
    EngineRouteError,
    EngineRouteRefusal,
    EngineRouteRefusalCode,
    RoutedOutcome,
    engine_route,
)
from cw.review_engine.route import is_non_deferrable_rule
from cw.review_findings import Finding

SEVERITIES = ("MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE")
RULES = ("style.naming", "coverage.tests", "acceptance.criteria")
OUTCOMES = ("fix", "reject", "defer", "operator_action")
FP = "fp-00000001"


def _finding(
    severity: str = "SHOULD_FIX",
    rule_id: str | None = "style.naming",
    fingerprint: str | None = FP,
    *,
    source: str = "engine",
    file: str = "src/a.py",
) -> Finding:
    return Finding.model_validate(
        {
            "severity": severity,
            "file": file,
            "line_start": 1,
            "line_end": 1,
            "summary": "s",
            "consequence": "c",
            "suggested_fix": "f",
            "evidence": "e",
            "confidence": "HIGH",
            "fingerprint": fingerprint,
            "rule_id": rule_id,
            "source": source,
        }
    )


def _adjudication(
    outcome: str, severity: str = "SHOULD_FIX", *, non_deferrable: bool = False
) -> Adjudication:
    return Adjudication.model_validate(
        {
            "severity": severity,
            "file": "src/a.py",
            "outcome": outcome,
            "rationale": "because",
            "fingerprint": FP,
            "non_deferrable": non_deferrable,
        }
    )


def _route(
    finding: Finding,
    adjudication: Adjudication,
    defers: frozenset[str] = frozenset(),
) -> RoutedOutcome | EngineRouteRefusal:
    return engine_route(finding, adjudication, defers)


@pytest.mark.parametrize("severity", SEVERITIES)
@pytest.mark.parametrize("rule_id", RULES)
@pytest.mark.parametrize("defers", [frozenset(), frozenset({FP})])
def test_reject_refused_for_every_severity_and_rule(
    severity: str, rule_id: str, defers: frozenset[str]
) -> None:
    routed = _route(
        _finding(severity, rule_id), _adjudication("reject", severity), defers
    )

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_REJECT_FORBIDDEN
    assert routed.file == "src/a.py"
    assert routed.fingerprint == FP


def test_no_engine_finding_can_end_with_reject() -> None:
    for severity, rule_id, outcome, defer, non_deferrable in itertools.product(
        SEVERITIES, RULES, OUTCOMES, (True, False), (True, False)
    ):
        defers = frozenset({FP}) if defer else frozenset()
        adjudication = _adjudication(outcome, severity, non_deferrable=non_deferrable)

        routed = _route(_finding(severity, rule_id), adjudication, defers)

        if isinstance(routed, RoutedOutcome):
            assert routed.outcome != "reject", (severity, rule_id, outcome)


@pytest.mark.parametrize("severity", SEVERITIES)
@pytest.mark.parametrize("rule_id", ["coverage.tests", "acceptance.criteria"])
@pytest.mark.parametrize("outcome", ["fix", "operator_action"])
def test_coverage_and_acceptance_rows_allow_fix_and_operator_action(
    severity: str, rule_id: str, outcome: str
) -> None:
    routed = _route(_finding(severity, rule_id), _adjudication(outcome, severity))

    assert isinstance(routed, RoutedOutcome)
    assert routed.outcome == outcome


@pytest.mark.parametrize("severity", SEVERITIES)
@pytest.mark.parametrize("rule_id", ["coverage.tests", "acceptance.criteria"])
def test_coverage_and_acceptance_defer_refused_even_with_operator_defer(
    severity: str, rule_id: str
) -> None:
    routed = _route(
        _finding(severity, rule_id),
        _adjudication("defer", severity),
        frozenset({FP}),
    )

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_NON_DEFERRABLE


@pytest.mark.parametrize("outcome", ["fix", "operator_action"])
def test_plan_deviation_non_deferrable_allows_fix_and_operator_action(
    outcome: str,
) -> None:
    routed = _route(_finding("SHOULD_FIX"), _adjudication(outcome, non_deferrable=True))

    assert isinstance(routed, RoutedOutcome)
    assert routed.outcome == outcome


def test_plan_deviation_non_deferrable_defer_refused() -> None:
    routed = _route(_finding("SHOULD_FIX"), _adjudication("defer", non_deferrable=True))

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_NON_DEFERRABLE


def test_must_fix_with_operator_defer_may_defer() -> None:
    routed = _route(
        _finding("MUST_FIX"), _adjudication("defer", "MUST_FIX"), frozenset({FP})
    )

    assert routed == RoutedOutcome(outcome="defer")


def test_must_fix_operator_defer_for_other_fingerprint_does_not_apply() -> None:
    routed = _route(
        _finding("MUST_FIX"),
        _adjudication("defer", "MUST_FIX"),
        frozenset({"fp-other-0001"}),
    )

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_MUST_FIX_DEFER_REQUIRES_OPERATOR


@pytest.mark.parametrize("outcome", ["fix", "operator_action"])
def test_must_fix_without_operator_defer_allows_fix_and_operator_action(
    outcome: str,
) -> None:
    routed = _route(_finding("MUST_FIX"), _adjudication(outcome, "MUST_FIX"))

    assert isinstance(routed, RoutedOutcome)
    assert routed.outcome == outcome


def test_must_fix_without_operator_defer_refuses_defer() -> None:
    routed = _route(_finding("MUST_FIX"), _adjudication("defer", "MUST_FIX"))

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_MUST_FIX_DEFER_REQUIRES_OPERATOR


@pytest.mark.parametrize("severity", ["SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"])
@pytest.mark.parametrize("outcome", ["fix", "defer", "operator_action"])
def test_other_severities_apply_as_recorded(severity: str, outcome: str) -> None:
    routed = _route(_finding(severity), _adjudication(outcome, severity))

    assert isinstance(routed, RoutedOutcome)
    assert routed.outcome == outcome


def test_precedence_coverage_must_fix_with_operator_defer_still_refused() -> None:
    routed = _route(
        _finding("MUST_FIX", "coverage.tests"),
        _adjudication("defer", "MUST_FIX"),
        frozenset({FP}),
    )

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_NON_DEFERRABLE


def test_precedence_reject_on_deferrable_finding_hits_row_one_first() -> None:
    routed = _route(_finding("NIT"), _adjudication("reject", "NIT"))

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.code is EngineRouteRefusalCode.ENGINE_REJECT_FORBIDDEN


def test_missing_fingerprint_refusal_names_blank() -> None:
    routed = _route(_finding("NIT", fingerprint=None), _adjudication("reject", "NIT"))

    assert isinstance(routed, EngineRouteRefusal)
    assert routed.fingerprint == ""


def test_builtin_finding_is_never_routed() -> None:
    """The caller filters on ``source``; builtin routing is unchanged.

    ``engine_route`` itself decides purely on rule/severity, so a builtin
    finding carrying no engine rule id and a recorded reject is not its input.
    """
    builtin = _finding("NIT", rule_id=None, fingerprint=None, source="builtin")
    assert builtin.source == "builtin"
    assert is_non_deferrable_rule(builtin.rule_id) is False


def test_is_non_deferrable_rule() -> None:
    assert is_non_deferrable_rule("coverage.x")
    assert is_non_deferrable_rule("acceptance.y")
    assert not is_non_deferrable_rule("style.x")
    assert not is_non_deferrable_rule(None)
    assert not is_non_deferrable_rule("")


def test_route_error_lists_every_offending_row_with_file_and_fingerprint() -> None:
    refusals = [
        EngineRouteRefusal(
            EngineRouteRefusalCode.ENGINE_REJECT_FORBIDDEN, "src/a.py", "fp-aaaaaaaa"
        ),
        EngineRouteRefusal(
            EngineRouteRefusalCode.ENGINE_NON_DEFERRABLE, "src/b.py", "fp-bbbbbbbb"
        ),
    ]

    error = EngineRouteError(refusals)

    assert error.refusals == tuple(refusals)
    assert str(error).splitlines() == [
        "engine_route: engine_reject_forbidden: src/a.py: fp-aaaaaaaa",
        "engine_route: engine_non_deferrable: src/b.py: fp-bbbbbbbb",
    ]


def test_package_import_does_not_pull_workspace_or_convergence() -> None:
    code = (
        "import sys; import cw.review_adjudication; "
        "bad = [m for m in ('cw.review_workspace', 'cw.review_convergence') "
        "if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
