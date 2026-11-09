"""Tests for ``cw.review_engine.mapping`` (RFC 0017 R10, #3039)."""

from __future__ import annotations

import json

import pytest

from cw.models.review_engine_summary import ReviewEngineSummary
from cw.review_engine.constants import (
    ENGINE_DEFAULT_CONSEQUENCE,
    ENGINE_DEFAULT_SUGGESTED_FIX,
    ENGINE_ROLE,
)
from cw.review_engine.mapping import engine_document, engine_finding_to_finding
from cw.review_findings import Finding, ReviewerFindingsDocument
from tests._review_engine_helpers import engine_finding, make_finding, make_summary


def _summary(*findings: dict[str, object], **overrides: object) -> ReviewEngineSummary:
    return ReviewEngineSummary.model_validate(make_summary(list(findings), **overrides))


def _mapped(**overrides: object) -> Finding:
    summary = _summary(make_finding(**overrides))
    return engine_finding_to_finding(summary.findings[0])


def test_one_to_one_fields() -> None:
    finding = _mapped(
        summary="S",
        evidence="E",
        file="src/x.py",
        line=7,
        consequence="C",
        suggested_fix="F",
    )

    assert finding.summary == "S"
    assert finding.evidence == "E"
    assert finding.file == "src/x.py"
    assert finding.consequence == "C"
    assert finding.suggested_fix == "F"
    assert finding.source == "engine"


def test_confidence_always_high() -> None:
    assert _mapped().confidence == "HIGH"


def test_defaults_for_missing_consequence_and_fix() -> None:
    finding = _mapped(consequence=None, suggested_fix=None)

    assert finding.consequence == ENGINE_DEFAULT_CONSEQUENCE
    assert finding.suggested_fix == ENGINE_DEFAULT_SUGGESTED_FIX


def test_no_file_maps_to_na_with_no_diff_anchor() -> None:
    finding = _mapped(file=None, line=None)

    assert finding.file == "N/A"
    assert finding.no_diff_anchor is True
    assert finding.line_start is None
    assert finding.line_end is None


def test_line_maps_to_start_and_end() -> None:
    finding = _mapped(line=12)

    assert (finding.line_start, finding.line_end) == (12, 12)
    assert finding.no_diff_anchor is False


def test_fingerprint_rule_id_source_engine_set() -> None:
    finding = _mapped(fingerprint="fp-abcdef01", rule_id="coverage.tests")

    assert finding.fingerprint == "fp-abcdef01"
    assert finding.rule_id == "coverage.tests"
    assert finding.source == "engine"


@pytest.mark.parametrize(
    "severity", ["MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"]
)
def test_every_severity_maps(severity: str) -> None:
    assert _mapped(severity=severity).severity == severity


def test_finding_dump_excludes_engine_fields_for_builtin() -> None:
    builtin = Finding(
        severity="NIT",
        file="a.py",
        summary="s",
        consequence="c",
        suggested_fix="f",
        evidence="e",
        confidence="HIGH",
    )

    dumped = builtin.model_dump(mode="json")

    assert "fingerprint" not in dumped
    assert "rule_id" not in dumped
    assert "source" not in dumped
    engine_dump = _mapped().model_dump(mode="json")
    assert engine_dump["source"] == "engine"
    assert engine_dump["fingerprint"] == "fp-000001"


def test_roundtrip_through_reviewer_findings_document() -> None:
    summary = _summary(
        engine_finding("MUST_FIX"), engine_finding("NIT", rule_id="style.other")
    )

    document = engine_document(summary, "repo-review")
    reloaded = ReviewerFindingsDocument.model_validate(
        json.loads(document.model_dump_json())
    )

    assert reloaded == document
    assert reloaded.reviewer_role == ENGINE_ROLE
    assert reloaded.status == "ok"
    assert [f.source for f in reloaded.findings] == ["engine", "engine"]
    assert "repo-review" in reloaded.detail
    assert "verdict=FAIL" in reloaded.detail
    assert "findings=2" in reloaded.detail
