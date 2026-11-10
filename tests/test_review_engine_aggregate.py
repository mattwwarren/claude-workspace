"""Tests for ``cw.review_engine.aggregate`` (RFC 0018 A4, #3216, Gate 0).

The recorded fixtures are verbatim ``-o`` outputs of a real ``codex exec``
aggregation turn (``tests/test_codex_contract_live.py::TestCodexEngineAggregation``
with ``CW_RECORD_ENGINE_FIXTURE=1``). They are never edited by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.review_engine.aggregate import (
    EngineAggregationOutput,
    assemble_summary,
    engine_aggregation_schema,
    render_aggregation_envelope,
)
from cw.review_engine.constants import ENGINE_AGGREGATOR_PREAMBLE
from cw.review_engine_summary_io import EngineSummaryError, load_review_engine_summary
from tests._review_engine_helpers import engine_finding

_FIXTURES = Path(__file__).parent / "fixtures"
_HEAD = "a" * 40


def _real(scenario: str) -> str:
    path = _FIXTURES / f"codex_engine_aggregation_real_{scenario}.json"
    return path.read_text(encoding="utf-8")


def _load_assembled(tmp_path: Path, output: EngineAggregationOutput) -> Path:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(assemble_summary(output, _HEAD)), encoding="utf-8")
    return path


class TestRecordedRealOutputs:
    def test_pass_output_validates_and_loads(self, tmp_path: Path) -> None:
        output = EngineAggregationOutput.model_validate_json(_real("pass"))
        summary = load_review_engine_summary(_load_assembled(tmp_path, output))
        assert summary.verdict == "PASS"
        assert summary.coverage == "COMPLETE"
        assert summary.findings == []
        assert summary.head_sha == _HEAD

    def test_fail_output_validates_and_loads(self, tmp_path: Path) -> None:
        output = EngineAggregationOutput.model_validate_json(_real("fail"))
        summary = load_review_engine_summary(_load_assembled(tmp_path, output))
        assert summary.verdict == "FAIL"
        severities = sorted(f.severity for f in summary.findings)
        assert severities == ["MUST_FIX", "SHOULD_FIX"]
        assert summary.counts.must_fix == 1
        assert summary.counts.should_fix == 1
        assert any(f.rule_id.startswith("coverage.") for f in summary.findings)

    def test_meta_names_the_codex_version(self) -> None:
        meta = json.loads(
            (_FIXTURES / "codex_engine_aggregation_real.meta.json").read_text(
                encoding="utf-8"
            )
        )
        assert set(meta) == {"pass", "fail"}
        for scenario, row in meta.items():
            assert row["scenario"] == scenario
            assert str(row["codex_version"]).startswith("codex-cli ")


class TestSchema:
    def test_schema_matches_the_accepted_golden(self) -> None:
        golden = json.loads(
            (_FIXTURES / "review_engine_aggregation_schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert engine_aggregation_schema() == golden

    def test_schema_is_strict_at_the_top_level(self) -> None:
        schema = engine_aggregation_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "verdict",
            "coverage",
            "risk",
            "risk_notes",
            "findings",
            "acceptance",
            "gate_signals",
        }


class TestAssembleSummary:
    def test_fills_the_three_cw_fields(self) -> None:
        output = EngineAggregationOutput.model_validate(
            {
                "verdict": "FAIL",
                "coverage": "COMPLETE",
                "risk": "MEDIUM",
                "risk_notes": ["one"],
                "findings": [
                    engine_finding("SHOULD_FIX"),
                    engine_finding("NIT", rule_id="style.other"),
                ],
                "acceptance": [],
                "gate_signals": [],
            }
        )
        summary = assemble_summary(output, _HEAD)
        assert summary["schema_version"] == 1
        assert summary["head_sha"] == _HEAD
        assert summary["counts"] == {
            "must_fix": 0,
            "should_fix": 1,
            "debt": 0,
            "nit": 1,
            "principle": 0,
        }
        assert summary["risk_notes"] == ["one"]

    def test_cross_field_rules_still_apply_after_assembly(self, tmp_path: Path) -> None:
        output = EngineAggregationOutput.model_validate(
            {
                "verdict": "PASS",
                "coverage": "COMPLETE",
                "risk": "LOW",
                "risk_notes": [],
                "findings": [engine_finding("MUST_FIX")],
                "acceptance": [],
                "gate_signals": [],
            }
        )
        with pytest.raises(EngineSummaryError) as excinfo:
            load_review_engine_summary(_load_assembled(tmp_path, output))
        assert excinfo.value.field_path == "verdict"


class TestOutputModel:
    def test_rejects_a_cw_owned_field(self) -> None:
        payload = json.loads(_real("pass"))
        payload["head_sha"] = _HEAD
        with pytest.raises(ValidationError):
            EngineAggregationOutput.model_validate(payload)

    def test_rejects_a_missing_field(self) -> None:
        payload = json.loads(_real("pass"))
        del payload["risk_notes"]
        with pytest.raises(ValidationError):
            EngineAggregationOutput.model_validate(payload)


class TestEnvelope:
    def test_order_and_content(self) -> None:
        envelope = render_aggregation_envelope(
            head_sha=_HEAD,
            aggregation_prompt="  Combine the documents.\n",
            role_documents=[("security", '{"a": 1}'), ("quality", '{"b": 2}')],
        )
        assert envelope.startswith(ENGINE_AGGREGATOR_PREAMBLE)
        assert f"Reviewed head: {_HEAD}" in envelope
        assert "## Aggregation Instructions\nCombine the documents." in envelope
        assert envelope.index("Role Document: security") < envelope.index(
            "Role Document: quality"
        )
        assert envelope.endswith("```\n")
