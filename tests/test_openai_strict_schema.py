"""Tests for cw.openai_strict_schema (#1364).

OpenAI's structured-output strict mode requires every object node
(``additionalProperties: false`` + full ``required``, with omitted/defaulted
fields wrapped nullable) — the raw ``ReviewerFindingsDocument.model_json_schema()``
dump satisfies neither, so every real ``codex exec --output-schema`` call 400s.
These tests drive ``to_openai_strict_schema`` against the REAL schema (not a
synthetic stand-in) so a future field addition to ``review_findings.py`` is
caught here if it breaks the transform's assumptions.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from cw.openai_strict_schema import to_openai_strict_schema
from cw.review_findings import ReviewerFindingsDocument

_VALID_FINDING: dict[str, Any] = {
    "severity": "MUST_FIX",
    "file": "src/cw/foo.py",
    "line_start": None,
    "line_end": None,
    "summary": "Bug here",
    "consequence": "It breaks",
    "suggested_fix": "Fix it",
    "evidence": "def broken():",
    "confidence": "HIGH",
    "escalation": None,
    "no_diff_anchor": None,
}


def _schema() -> dict[str, Any]:
    return ReviewerFindingsDocument.model_json_schema()


def _walk_all_dicts(node: object) -> list[dict[str, Any]]:
    """Flatten every dict reachable from *node* (including itself)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk_all_dicts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_all_dicts(item))
    return found


class TestTopLevelAndDefsShape:
    def test_top_level_gets_additional_properties_false(self) -> None:
        result = to_openai_strict_schema(_schema())
        assert result["additionalProperties"] is False

    def test_every_defs_object_gets_additional_properties_false(self) -> None:
        result = to_openai_strict_schema(_schema())
        assert result["$defs"]["Finding"]["additionalProperties"] is False
        assert result["$defs"]["EscalationMetadata"]["additionalProperties"] is False

    def test_required_equals_all_properties_keys_at_every_object_node(self) -> None:
        result = to_openai_strict_schema(_schema())
        nodes = [
            result,
            result["$defs"]["Finding"],
            result["$defs"]["EscalationMetadata"],
        ]
        for node in nodes:
            assert set(node["required"]) == set(node["properties"].keys())

    def test_default_keyword_stripped_everywhere(self) -> None:
        result = to_openai_strict_schema(_schema())
        for node in _walk_all_dicts(result):
            assert "default" not in node


class TestNullableWrapping:
    def test_previously_optional_scalar_field_becomes_nullable(self) -> None:
        result = to_openai_strict_schema(_schema())
        assert result["properties"]["detail"] == {
            "anyOf": [{"title": "Detail", "type": "string"}, {"type": "null"}]
        }

    def test_previously_optional_container_field_becomes_nullable(self) -> None:
        result = to_openai_strict_schema(_schema())
        assert result["properties"]["findings"] == {
            "anyOf": [
                {
                    "items": {"$ref": "#/$defs/Finding"},
                    "title": "Findings",
                    "type": "array",
                },
                {"type": "null"},
            ]
        }

    def test_already_nullable_fields_not_double_wrapped(self) -> None:
        result = to_openai_strict_schema(_schema())
        finding = result["$defs"]["Finding"]["properties"]
        for name in ("line_start", "line_end", "escalation"):
            sub = finding[name]
            assert len(sub["anyOf"]) == 2
            assert "default" not in sub


class TestTransformProperties:
    def test_transform_does_not_mutate_input(self) -> None:
        original = _schema()
        snapshot = copy.deepcopy(original)
        to_openai_strict_schema(original)
        assert original == snapshot

    def test_idempotent(self) -> None:
        once = to_openai_strict_schema(_schema())
        twice = to_openai_strict_schema(once)
        assert twice == once


class TestRoundTripValidation:
    def test_round_trip_model_validate_null_detail(self) -> None:
        # status="ok" with a non-empty findings list (#1806: status="degraded"
        # now requires a stated reason, so it no longer tolerates the blank
        # detail this null->"" coercion produces): these fixtures exist to
        # verify field-level null->"" coercion, orthogonal to either
        # cross-field justification rule.
        payload = {
            "reviewer_role": "R",
            "status": "ok",
            "detail": None,
            "findings": [_VALID_FINDING],
        }
        doc = ReviewerFindingsDocument.model_validate(payload)
        assert doc.detail == ""

    def test_round_trip_model_validate_null_findings(self) -> None:
        payload = {
            "reviewer_role": "R",
            "status": "degraded",
            "detail": "sandbox lacked filesystem access",
            "findings": None,
        }
        doc = ReviewerFindingsDocument.model_validate(payload)
        assert doc.findings == []

    def test_round_trip_model_validate_both_null(self) -> None:
        # #1806: both field-level null->"" (detail) and null->[] (findings)
        # coercions still run first, but the resulting blank detail on a
        # degraded status is then rejected by the new model-level
        # degraded/failed-reason validator -- this used to assert a
        # successful round-trip, which was exactly the gap #1806 closes.
        payload = {
            "reviewer_role": "R",
            "status": "degraded",
            "detail": None,
            "findings": None,
        }
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument.model_validate(payload)

    def test_round_trip_model_validate_null_optional_finding_fields(self) -> None:
        payload = {
            "reviewer_role": "R",
            "status": "ok",
            "detail": "",
            "findings": [_VALID_FINDING],
        }
        doc = ReviewerFindingsDocument.model_validate(payload)
        assert len(doc.findings) == 1
        assert doc.findings[0].line_start is None
        assert doc.findings[0].line_end is None
        assert doc.findings[0].escalation is None

    def test_round_trip_model_validate_null_no_diff_anchor(self) -> None:
        # #1817 added `no_diff_anchor: bool = False` to Finding without a
        # matching None-normalizer (unlike detail/findings on the document).
        # The strict-schema transform makes it nullable+required, so codex
        # faithfully sends `null` on every finding that doesn't use the
        # no-diff-anchor marker — and model_validate rejected `null` for a
        # `bool` field, classifying the entire reviewer document as
        # schema_mismatch. This test would have caught the gap at #1817
        # time if it had existed then.
        payload = {
            "reviewer_role": "R",
            "status": "ok",
            "detail": "",
            "findings": [{**_VALID_FINDING, "no_diff_anchor": None}],
        }
        doc = ReviewerFindingsDocument.model_validate(payload)
        assert doc.findings[0].no_diff_anchor is False

    def test_non_null_values_still_validate_normally(self) -> None:
        payload = {
            "reviewer_role": "R",
            "status": "ok",
            "detail": "some text",
            "findings": [_VALID_FINDING],
        }
        doc = ReviewerFindingsDocument.model_validate(payload)
        assert doc.detail == "some text"
        assert len(doc.findings) == 1
        assert doc.findings[0].summary == "Bug here"
