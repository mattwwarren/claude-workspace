"""Tests for the strict review engine summary model (#3036, RFC 0017 S1)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from cw.models.review_engine_summary import (
    ACCEPTANCE_RULE_PREFIX,
    COVERAGE_RULE_PREFIX,
    INCONSISTENT_ERROR_TYPES,
    INCONSISTENT_MESSAGES,
    MAX_ENGINE_ACCEPTANCE_EVIDENCE_LENGTH,
    MAX_ENGINE_ACCEPTANCE_ID_LENGTH,
    MAX_ENGINE_ACCEPTANCE_ROWS,
    MAX_ENGINE_CONSEQUENCE_LENGTH,
    MAX_ENGINE_EVIDENCE_LENGTH,
    MAX_ENGINE_FILE_LENGTH,
    MAX_ENGINE_FINDING_SUMMARY_LENGTH,
    MAX_ENGINE_FINDINGS,
    MAX_ENGINE_FINGERPRINT_LENGTH,
    MAX_ENGINE_GATE_DETAIL_LENGTH,
    MAX_ENGINE_GATE_NAME_LENGTH,
    MAX_ENGINE_GATE_SIGNALS,
    MAX_ENGINE_RISK_NOTE_LENGTH,
    MAX_ENGINE_RISK_NOTES,
    MAX_ENGINE_RULE_ID_LENGTH,
    MAX_ENGINE_SUGGESTED_FIX_LENGTH,
    MIN_ENGINE_FINGERPRINT_LENGTH,
    REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
    AcceptanceRow,
    AcceptanceStatus,
    EngineCounts,
    EngineCoverage,
    EngineFinding,
    EngineRisk,
    EngineSeverity,
    EngineVerdict,
    GateSignal,
    GateSignalStatus,
    ReviewEngineSummary,
)
from pydantic import ValidationError

from cw.cli.review import engine as engine_cli
from cw.models import review_engine_summary as model_mod
from cw.models.review_feedback import ReviewFeedback
from cw.review_findings._models import Severity
from tests._review_engine_helpers import SHA, make_finding, make_summary
from tests.conftest import _norm, _section_between

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_core import ErrorDetails

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _validate(payload: dict[str, object]) -> ReviewEngineSummary:
    return ReviewEngineSummary.model_validate(payload)


def _errors(payload: dict[str, object]) -> list[ErrorDetails]:
    with pytest.raises(ValidationError) as excinfo:
        _validate(payload)
    return excinfo.value.errors()


def _only_error(payload: dict[str, object]) -> ErrorDetails:
    errors = _errors(payload)
    assert len(errors) == 1, errors
    return errors[0]


def _finding(index: int, **overrides: object) -> dict[str, object]:
    return make_finding(fingerprint=f"fp-{index:06d}", **overrides)


def _acceptance_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"id": "ac1", "status": "PASS", "evidence": "tested"}
    row.update(overrides)
    return row


def _gate_signal(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"name": "ruff", "status": "PASS", "detail": "clean"}
    row.update(overrides)
    return row


class TestValid:
    def test_full_summary_round_trips(self) -> None:
        payload = make_summary(
            [
                _finding(
                    1, severity="MUST_FIX", consequence="breaks", suggested_fix="x"
                ),
                _finding(2, severity="DEBT", file=None, line=None),
            ],
            risk="HIGH",
            risk_notes=["touches auth"],
            acceptance=[_acceptance_row(), _acceptance_row(id="ac2", evidence=None)],
            gate_signals=[_gate_signal(), _gate_signal(name="mypy", detail=None)],
        )
        summary = _validate(payload)

        restored = ReviewEngineSummary.model_validate_json(summary.model_dump_json())

        assert restored == summary
        assert summary.verdict == "FAIL"
        assert summary.counts.must_fix == 1
        assert summary.counts.debt == 1

    def test_minimal_summary_accepted(self) -> None:
        summary = _validate(make_summary())

        assert summary.findings == []
        assert summary.acceptance == []
        assert summary.gate_signals == []
        assert summary.risk_notes == []

    def test_finding_without_location_accepted(self) -> None:
        summary = _validate(make_summary([make_finding(file=None, line=None)]))

        assert summary.findings[0].file is None
        assert summary.findings[0].line is None

    def test_pass_with_partial_coverage_accepted(self) -> None:
        summary = _validate(make_summary(coverage="PARTIAL"))

        assert summary.verdict == "PASS"
        assert summary.coverage == "PARTIAL"

    def test_fail_without_gating_finding_accepted(self) -> None:
        summary = _validate(make_summary([make_finding()], verdict="FAIL"))

        assert summary.verdict == "FAIL"


class TestRequiredNullableKeys:
    @pytest.mark.parametrize("key", ["file", "line"])
    def test_finding_key_omitted_rejected(self, key: str) -> None:
        finding = make_finding()
        del finding[key]

        error = _only_error(make_summary([finding]))

        assert error["type"] == "missing"
        assert error["loc"] == ("findings", 0, key)

    def test_acceptance_evidence_omitted_rejected(self) -> None:
        row = _acceptance_row()
        del row["evidence"]

        error = _only_error(make_summary(acceptance=[row]))

        assert error["type"] == "missing"
        assert error["loc"] == ("acceptance", 0, "evidence")

    def test_gate_detail_omitted_rejected(self) -> None:
        row = _gate_signal()
        del row["detail"]

        error = _only_error(make_summary(gate_signals=[row]))

        assert error["type"] == "missing"
        assert error["loc"] == ("gate_signals", 0, "detail")

    @pytest.mark.parametrize(
        "key", ["risk_notes", "findings", "acceptance", "gate_signals"]
    )
    def test_top_level_list_omitted_rejected(self, key: str) -> None:
        payload = make_summary()
        del payload[key]

        error = _only_error(payload)

        assert error["type"] == "missing"
        assert error["loc"] == (key,)

    def test_explicit_nulls_accepted(self) -> None:
        summary = _validate(
            make_summary(
                [make_finding(file=None, line=None)],
                acceptance=[_acceptance_row(evidence=None)],
                gate_signals=[_gate_signal(detail=None)],
            )
        )

        assert summary.acceptance[0].evidence is None
        assert summary.gate_signals[0].detail is None

    @pytest.mark.parametrize("key", ["consequence", "suggested_fix"])
    def test_optional_finding_key_may_be_omitted(self, key: str) -> None:
        finding = make_finding()
        del finding[key]

        summary = _validate(make_summary([finding]))

        assert getattr(summary.findings[0], key) is None


class TestExtraForbid:
    @pytest.mark.parametrize(
        ("mutate", "loc"),
        [
            (lambda p: p.__setitem__("bogus", 1), ("bogus",)),
            (
                lambda p: p["findings"][0].__setitem__("bogus", 1),
                ("findings", 0, "bogus"),
            ),
            (lambda p: p["counts"].__setitem__("bogus", 0), ("counts", "bogus")),
            (
                lambda p: p["acceptance"][0].__setitem__("bogus", 1),
                ("acceptance", 0, "bogus"),
            ),
            (
                lambda p: p["gate_signals"][0].__setitem__("bogus", 1),
                ("gate_signals", 0, "bogus"),
            ),
        ],
        ids=["top", "finding", "counts", "acceptance", "gate"],
    )
    def test_unknown_key_rejected(
        self, mutate: Callable[[dict[str, object]], None], loc: tuple[object, ...]
    ) -> None:
        payload = make_summary(
            [make_finding()],
            acceptance=[_acceptance_row()],
            gate_signals=[_gate_signal()],
        )
        mutate(payload)

        error = _only_error(payload)

        assert error["type"] == "extra_forbidden"
        assert error["loc"] == loc


class TestStrict:
    @pytest.mark.parametrize("value", [True, "1", 1.0])
    def test_schema_version_not_coerced(self, value: object) -> None:
        error = _only_error(make_summary(schema_version=value))

        assert error["loc"] == ("schema_version",)

    def test_count_not_coerced(self) -> None:
        payload = make_summary()
        counts = payload["counts"]
        assert isinstance(counts, dict)
        counts["nit"] = "1"

        error = _only_error(payload)

        assert error["loc"] == ("counts", "nit")


class TestVocabularies:
    def test_literals_are_uppercase_and_exact(self) -> None:
        assert get_args(EngineSeverity) == (
            "MUST_FIX",
            "SHOULD_FIX",
            "DEBT",
            "NIT",
            "PRINCIPLE",
        )
        assert get_args(EngineVerdict) == ("PASS", "FAIL")
        assert get_args(EngineCoverage) == ("COMPLETE", "PARTIAL", "FAILED")
        assert get_args(EngineRisk) == ("LOW", "MEDIUM", "HIGH")
        assert get_args(AcceptanceStatus) == ("PASS", "FAIL", "NOT_APPLICABLE")
        assert get_args(GateSignalStatus) == ("PASS", "FAIL", "WARN")
        for alias in (
            EngineSeverity,
            EngineVerdict,
            EngineCoverage,
            EngineRisk,
            AcceptanceStatus,
            GateSignalStatus,
        ):
            for member in get_args(alias):
                assert member == member.upper()

    @pytest.mark.parametrize(
        ("build", "loc"),
        [
            (lambda: make_summary(verdict="pass"), ("verdict",)),
            (lambda: make_summary(coverage="complete"), ("coverage",)),
            (lambda: make_summary(risk="low"), ("risk",)),
            (
                lambda: make_summary(
                    [make_finding(severity="nit")],
                    counts={
                        "must_fix": 0,
                        "should_fix": 0,
                        "debt": 0,
                        "nit": 0,
                        "principle": 0,
                    },
                ),
                ("findings", 0, "severity"),
            ),
            (
                lambda: make_summary(acceptance=[_acceptance_row(status="pass")]),
                ("acceptance", 0, "status"),
            ),
            (
                lambda: make_summary(gate_signals=[_gate_signal(status="pass")]),
                ("gate_signals", 0, "status"),
            ),
        ],
        ids=["verdict", "coverage", "risk", "severity", "acceptance", "gate"],
    )
    def test_lowercase_alias_rejected(
        self, build: Callable[[], dict[str, object]], loc: tuple[object, ...]
    ) -> None:
        error = _only_error(build())

        assert error["type"] == "literal_error"
        assert error["loc"] == loc

    def test_severity_matches_review_findings(self) -> None:
        assert get_args(EngineSeverity) == get_args(Severity)

    def test_counts_fields_track_severity(self) -> None:
        assert list(EngineCounts.model_fields) == [
            member.lower() for member in get_args(EngineSeverity)
        ]


class TestHeadSha:
    def test_valid(self) -> None:
        assert _validate(make_summary()).head_sha == SHA

    @pytest.mark.parametrize(
        "value",
        ["A" * 40, "a" * 39, "a" * 41, "g" * 40],
        ids=["upper", "39", "41", "hex"],
    )
    def test_invalid_rejected(self, value: str) -> None:
        error = _only_error(make_summary(head_sha=value))

        assert error["loc"] == ("head_sha",)

    def test_pattern_matches_review_feedback(self) -> None:
        def pattern_of(field: object) -> str:
            metadata = getattr(field, "metadata", [])
            patterns = [m.pattern for m in metadata if getattr(m, "pattern", None)]
            assert len(patterns) == 1
            return str(patterns[0])

        assert pattern_of(ReviewEngineSummary.model_fields["head_sha"]) == pattern_of(
            ReviewFeedback.model_fields["reviewed_sha"]
        )


class TestFingerprint:
    @pytest.mark.parametrize(
        "value", ["a" * 8, "a" * 128, "Az09._:-"], ids=["min", "max", "charset"]
    )
    def test_accepted(self, value: str) -> None:
        summary = _validate(make_summary([make_finding(fingerprint=value)]))

        assert summary.findings[0].fingerprint == value

    @pytest.mark.parametrize(
        "value",
        ["a" * 7, "a" * 129, "fp 000001", "fp/000001", ""],
        ids=["7", "129", "space", "slash", "empty"],
    )
    def test_rejected(self, value: str) -> None:
        error = _only_error(make_summary([make_finding(fingerprint=value)]))

        assert error["loc"] == ("findings", 0, "fingerprint")


class TestRuleId:
    @pytest.mark.parametrize(
        "value", ["coverage.incomplete", "acceptance.ac1", "a", "a.b-c"]
    )
    def test_accepted(self, value: str) -> None:
        summary = _validate(make_summary([make_finding(rule_id=value)]))

        assert summary.findings[0].rule_id == value

    @pytest.mark.parametrize("value", ["Style.naming", ".a", "a.", ""])
    def test_rejected(self, value: str) -> None:
        error = _only_error(make_summary([make_finding(rule_id=value)]))

        assert error["loc"] == ("findings", 0, "rule_id")

    def test_reserved_prefixes(self) -> None:
        assert COVERAGE_RULE_PREFIX == "coverage."
        assert ACCEPTANCE_RULE_PREFIX == "acceptance."
        for prefix in (COVERAGE_RULE_PREFIX, ACCEPTANCE_RULE_PREFIX):
            finding = make_finding(rule_id=f"{prefix}x")
            assert (
                _validate(make_summary([finding])).findings[0].rule_id == f"{prefix}x"
            )


class TestFileAndLine:
    @pytest.mark.parametrize("value", ["", "   ", "N/A", "n/a", " N/a "])
    def test_bad_file_rejected(self, value: str) -> None:
        error = _only_error(make_summary([make_finding(file=value)]))

        assert error["loc"] == ("findings", 0, "file")
        assert error["type"] not in INCONSISTENT_ERROR_TYPES

    def test_zero_line_rejected(self) -> None:
        error = _only_error(make_summary([make_finding(line=0)]))

        assert error["loc"] == ("findings", 0, "line")

    def test_line_without_file_rejected(self) -> None:
        error = _only_error(make_summary([make_finding(file=None, line=3)]))

        assert error["type"] == "engine_line_without_file"
        assert error["type"] in INCONSISTENT_ERROR_TYPES
        assert error["loc"] == ("findings", 0)
        assert error.get("ctx", {})["field_path"] == "line"

    def test_file_without_line_accepted(self) -> None:
        summary = _validate(make_summary([make_finding(line=None)]))

        assert summary.findings[0].file == "src/a.py"


class TestTextFields:
    @pytest.mark.parametrize(
        "key", ["summary", "evidence", "consequence", "suggested_fix"]
    )
    def test_blank_finding_text_rejected(self, key: str) -> None:
        error = _only_error(make_summary([make_finding(**{key: "  \n"})]))

        assert error["loc"] == ("findings", 0, key)

    @pytest.mark.parametrize("key", ["consequence", "suggested_fix"])
    def test_null_optional_text_accepted(self, key: str) -> None:
        summary = _validate(make_summary([make_finding(**{key: None})]))

        assert getattr(summary.findings[0], key) is None

    @pytest.mark.parametrize(
        ("build", "loc"),
        [
            (
                lambda: make_summary(acceptance=[_acceptance_row(evidence=" ")]),
                ("acceptance", 0, "evidence"),
            ),
            (
                lambda: make_summary(acceptance=[_acceptance_row(id=" ")]),
                ("acceptance", 0, "id"),
            ),
            (
                lambda: make_summary(gate_signals=[_gate_signal(detail=" ")]),
                ("gate_signals", 0, "detail"),
            ),
            (
                lambda: make_summary(gate_signals=[_gate_signal(name="")]),
                ("gate_signals", 0, "name"),
            ),
            (lambda: make_summary(risk_notes=["ok", " "]), ("risk_notes", 1)),
        ],
        ids=[
            "acceptance-evidence",
            "acceptance-id",
            "gate-detail",
            "gate-name",
            "note",
        ],
    )
    def test_blank_row_text_rejected(
        self, build: Callable[[], dict[str, object]], loc: tuple[object, ...]
    ) -> None:
        error = _only_error(build())

        assert error["loc"] == loc


def _findings_summary(count: int) -> dict[str, object]:
    return make_summary([_finding(i) for i in range(count)])


def _one_finding(**fields: object) -> dict[str, object]:
    return make_summary([make_finding(**fields)])


_BOUNDS: list[tuple[str, int, Callable[[int], dict[str, object]]]] = [
    ("findings", MAX_ENGINE_FINDINGS, _findings_summary),
    (
        "acceptance",
        MAX_ENGINE_ACCEPTANCE_ROWS,
        lambda n: make_summary(
            acceptance=[_acceptance_row(id=f"ac{i}") for i in range(n)]
        ),
    ),
    (
        "gate_signals",
        MAX_ENGINE_GATE_SIGNALS,
        lambda n: make_summary(
            gate_signals=[_gate_signal(name=f"g{i}") for i in range(n)]
        ),
    ),
    ("risk_notes", MAX_ENGINE_RISK_NOTES, lambda n: make_summary(risk_notes=["n"] * n)),
    (
        "risk_note",
        MAX_ENGINE_RISK_NOTE_LENGTH,
        lambda n: make_summary(risk_notes=["n" * n]),
    ),
    (
        "finding.summary",
        MAX_ENGINE_FINDING_SUMMARY_LENGTH,
        lambda n: _one_finding(summary="s" * n),
    ),
    (
        "finding.evidence",
        MAX_ENGINE_EVIDENCE_LENGTH,
        lambda n: _one_finding(evidence="e" * n),
    ),
    (
        "finding.consequence",
        MAX_ENGINE_CONSEQUENCE_LENGTH,
        lambda n: _one_finding(consequence="c" * n),
    ),
    (
        "finding.suggested_fix",
        MAX_ENGINE_SUGGESTED_FIX_LENGTH,
        lambda n: _one_finding(suggested_fix="f" * n),
    ),
    ("finding.file", MAX_ENGINE_FILE_LENGTH, lambda n: _one_finding(file="f" * n)),
    (
        "finding.fingerprint",
        MAX_ENGINE_FINGERPRINT_LENGTH,
        lambda n: _one_finding(fingerprint="p" * n),
    ),
    (
        "finding.rule_id",
        MAX_ENGINE_RULE_ID_LENGTH,
        lambda n: _one_finding(rule_id="r" * n),
    ),
    (
        "acceptance.id",
        MAX_ENGINE_ACCEPTANCE_ID_LENGTH,
        lambda n: make_summary(acceptance=[_acceptance_row(id="i" * n)]),
    ),
    (
        "acceptance.evidence",
        MAX_ENGINE_ACCEPTANCE_EVIDENCE_LENGTH,
        lambda n: make_summary(acceptance=[_acceptance_row(evidence="e" * n)]),
    ),
    (
        "gate.name",
        MAX_ENGINE_GATE_NAME_LENGTH,
        lambda n: make_summary(gate_signals=[_gate_signal(name="g" * n)]),
    ),
    (
        "gate.detail",
        MAX_ENGINE_GATE_DETAIL_LENGTH,
        lambda n: make_summary(gate_signals=[_gate_signal(detail="d" * n)]),
    ),
]


class TestBounds:
    @pytest.mark.parametrize(
        ("cap", "build"),
        [(cap, build) for _name, cap, build in _BOUNDS],
        ids=[name for name, _cap, _build in _BOUNDS],
    )
    def test_cap_accepted_and_cap_plus_one_rejected(
        self, cap: int, build: Callable[[int], dict[str, object]]
    ) -> None:
        _validate(build(cap))

        errors = _errors(build(cap + 1))

        assert all(err["type"] not in INCONSISTENT_ERROR_TYPES for err in errors)

    def test_fingerprint_minimum(self) -> None:
        _validate(_one_finding(fingerprint="p" * MIN_ENGINE_FINGERPRINT_LENGTH))

        error = _only_error(
            _one_finding(fingerprint="p" * (MIN_ENGINE_FINGERPRINT_LENGTH - 1))
        )

        assert error["type"] not in INCONSISTENT_ERROR_TYPES
        assert error["loc"] == ("findings", 0, "fingerprint")


class TestCounts:
    def test_counts_is_typed(self) -> None:
        summary = _validate(make_summary([make_finding()]))

        assert isinstance(summary.counts, EngineCounts)
        assert summary.counts.nit == 1

    def test_missing_count_key_rejected(self) -> None:
        payload = make_summary()
        counts = payload["counts"]
        assert isinstance(counts, dict)
        del counts["debt"]

        error = _only_error(payload)

        assert error["type"] == "missing"
        assert error["loc"] == ("counts", "debt")

    def test_negative_rejected(self) -> None:
        payload = make_summary()
        counts = payload["counts"]
        assert isinstance(counts, dict)
        counts["debt"] = -1

        error = _only_error(payload)

        assert error["loc"] == ("counts", "debt")

    def test_mismatch_rejected(self) -> None:
        payload = make_summary(
            [make_finding()],
            counts={
                "must_fix": 0,
                "should_fix": 0,
                "debt": 1,
                "nit": 0,
                "principle": 0,
            },
        )

        error = _only_error(payload)

        assert error["type"] == "engine_counts_mismatch"
        assert error.get("ctx", {})["field_path"] == "counts"

    def test_tally(self) -> None:
        findings = [
            EngineFinding.model_validate(_finding(1, severity="MUST_FIX")),
            EngineFinding.model_validate(_finding(2, severity="MUST_FIX")),
            EngineFinding.model_validate(_finding(3, severity="PRINCIPLE")),
        ]

        tally = EngineCounts.tally(findings)

        assert tally == EngineCounts(
            must_fix=2, should_fix=0, debt=0, nit=0, principle=1
        )


_ZERO_COUNTS = {"must_fix": 0, "should_fix": 0, "debt": 0, "nit": 0, "principle": 0}


class TestConsistency:
    def test_duplicate_fingerprint_rejected(self) -> None:
        payload = make_summary([_finding(0), _finding(1), _finding(1)])

        error = _only_error(payload)

        assert error["type"] == "engine_duplicate_fingerprint"
        assert error.get("ctx", {})["field_path"] == "findings.2.fingerprint"

    @pytest.mark.parametrize("severity", ["MUST_FIX", "SHOULD_FIX"])
    def test_pass_with_gating_finding_rejected(self, severity: str) -> None:
        payload = make_summary([make_finding(severity=severity)], verdict="PASS")

        error = _only_error(payload)

        assert error["type"] == "engine_pass_with_gating_finding"
        assert error.get("ctx", {})["field_path"] == "verdict"

    @pytest.mark.parametrize("severity", ["DEBT", "NIT", "PRINCIPLE"])
    def test_pass_with_non_gating_finding_accepted(self, severity: str) -> None:
        summary = _validate(make_summary([make_finding(severity=severity)]))

        assert summary.verdict == "PASS"

    def _rendered(self) -> dict[str, tuple[ErrorDetails, str]]:
        evidence_text = "SECRET-EVIDENCE-TEXT"
        hidden = "SECRET-SUMMARY-TEXT"
        cases: dict[str, tuple[dict[str, object], str]] = {
            "engine_duplicate_fingerprint": (
                make_summary(
                    [
                        make_finding(evidence=evidence_text, summary=hidden),
                        make_finding(evidence=evidence_text, summary=hidden),
                    ]
                ),
                "duplicate fingerprint 'fp-000001'",
            ),
            "engine_counts_mismatch": (
                make_summary(
                    [
                        _finding(
                            1, severity="DEBT", evidence=evidence_text, summary=hidden
                        ),
                        _finding(2, evidence=evidence_text, summary=hidden),
                    ],
                    counts={**_ZERO_COUNTS, "nit": 2},
                ),
                "counts must_fix=0 should_fix=0 debt=0 nit=2 principle=0 do not "
                "match findings tally must_fix=0 should_fix=0 debt=1 nit=1 "
                "principle=0",
            ),
            "engine_pass_with_gating_finding": (
                make_summary(
                    [
                        make_finding(
                            severity="MUST_FIX", evidence=evidence_text, summary=hidden
                        )
                    ],
                    verdict="PASS",
                ),
                "verdict PASS with 1 MUST_FIX/SHOULD_FIX finding(s)",
            ),
            "engine_line_without_file": (
                make_summary(
                    [make_finding(file=None, evidence=evidence_text, summary=hidden)]
                ),
                "line 3 given without file",
            ),
        }
        return {
            error_type: (_only_error(payload), expected)
            for error_type, (payload, expected) in cases.items()
        }

    def test_rendered_messages_pin_templates(self) -> None:
        rendered = self._rendered()

        assert set(rendered) == INCONSISTENT_ERROR_TYPES
        for error_type, (error, expected) in rendered.items():
            assert error["type"] == error_type
            assert error["type"] in INCONSISTENT_ERROR_TYPES
            assert error["msg"] == expected
            assert "{" not in error["msg"]
            assert "}" not in error["msg"]
            assert "SECRET" not in error["msg"]


class TestMessageTemplates:
    def test_templates_are_verbatim(self) -> None:
        assert INCONSISTENT_MESSAGES == {
            "engine_duplicate_fingerprint": "duplicate fingerprint {fingerprint!r}",
            "engine_counts_mismatch": (
                "counts {declared} do not match findings tally {actual}"
            ),
            "engine_pass_with_gating_finding": (
                "verdict PASS with {count} MUST_FIX/SHOULD_FIX finding(s)"
            ),
            "engine_line_without_file": "line {line} given without file",
        }
        assert frozenset(INCONSISTENT_MESSAGES) == INCONSISTENT_ERROR_TYPES


class TestSchemaVersionDirect:
    def test_version_two_rejected(self) -> None:
        error = _only_error(make_summary(schema_version=2))

        assert error["loc"] == ("schema_version",)
        assert error["type"] not in INCONSISTENT_ERROR_TYPES

    def test_version_one_accepted(self) -> None:
        summary = _validate(make_summary(schema_version=1))

        assert summary.schema_version == REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION == 1


class TestJsonSchema:
    def test_defs_and_descriptions(self) -> None:
        schema = ReviewEngineSummary.model_json_schema()
        defs = schema["$defs"]

        for name in ("EngineFinding", "EngineCounts", "AcceptanceRow", "GateSignal"):
            assert name in defs
        for owner in (schema, *(defs[name] for name in defs)):
            assert owner["properties"]
            for field_name, prop in owner["properties"].items():
                assert prop.get("description"), field_name

    def test_models_forbid_extra(self) -> None:
        for model in (
            ReviewEngineSummary,
            EngineFinding,
            EngineCounts,
            AcceptanceRow,
            GateSignal,
        ):
            assert model.model_config.get("extra") == "forbid"
            assert model.model_config.get("strict") is True


_CONSTANT_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]+)` \| `([^`]*)` \|")
_VOCAB_ROW = re.compile(
    r"^\| `(EngineSeverity|EngineVerdict|EngineCoverage|EngineRisk|"
    r"AcceptanceStatus|GateSignalStatus)` \| (.+?) \|"
)
_SECTION_HEADING = "## 12. Review Engine Summary (RFC 0017)"


def _engine_section() -> str:
    return _section_between(_read("docs/headless-contract.md"), _SECTION_HEADING)


def _code_constant(name: str) -> object:
    from cw import review_engine_summary_io as io_mod

    for module in (model_mod, io_mod, engine_cli):
        if hasattr(module, name):
            return getattr(module, name)
    msg = f"documented constant {name} is not defined in code"
    raise AssertionError(msg)


class TestHeadlessContractDocs:
    def test_constants_table_matches_code(self) -> None:
        documented = {
            match.group(1): match.group(2)
            for line in _engine_section().splitlines()
            if (match := _CONSTANT_ROW.match(line))
        }
        expected_names = {
            name
            for name in dir(model_mod)
            if re.fullmatch(r"(MAX|MIN)_ENGINE_\w+|ENGINE_\w+_RE|\w+_RULE_PREFIX", name)
        } | {
            "REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION",
            "MAX_ISSUE_TEXT_CHARS",
            "MAX_VALIDATE_ERROR_LINES",
        }

        assert expected_names <= set(documented)
        for name, value in documented.items():
            assert value == str(_code_constant(name)), name

    def test_message_templates_quoted_verbatim(self) -> None:
        section = _norm(_engine_section())

        for template in INCONSISTENT_MESSAGES.values():
            assert template in section

    def test_version_history_first_row(self) -> None:
        rows = [
            line for line in _engine_section().splitlines() if line.startswith("| 1 |")
        ]

        assert rows
        assert "#3036" in rows[0]

    def test_section_names_prefixes_command_and_exits(self) -> None:
        section = _norm(_engine_section())

        assert f"`{COVERAGE_RULE_PREFIX}`" in section
        assert f"`{ACCEPTANCE_RULE_PREFIX}`" in section
        assert "cw schema show review-engine-summary" in section
        assert "cw review engine validate" in section
        for code in ("Exit 0", "Exit 1", "Exit 2"):
            assert code in section

    def test_vocabularies_match_code(self) -> None:
        aliases = {
            "EngineSeverity": EngineSeverity,
            "EngineVerdict": EngineVerdict,
            "EngineCoverage": EngineCoverage,
            "EngineRisk": EngineRisk,
            "AcceptanceStatus": AcceptanceStatus,
            "GateSignalStatus": GateSignalStatus,
        }
        documented = {
            match.group(1): tuple(re.findall(r"`([A-Z_]+)`", match.group(2)))
            for line in _engine_section().splitlines()
            if (match := _VOCAB_ROW.match(line))
        }

        assert set(documented) == set(aliases)
        for name, alias in aliases.items():
            assert documented[name] == get_args(alias), name

    def test_section_8_untouched(self) -> None:
        text = _read("docs/headless-contract.md")

        assert "review-engine" not in _section_between(text, "## 8. Versioning")

    def test_no_attestation_rule(self) -> None:
        section = _engine_section()

        assert "attestation" not in section.lower()
        assert "II1" not in section


def test_builder_payload_is_not_mutated_by_validation() -> None:
    payload = make_summary([make_finding()])
    snapshot = copy.deepcopy(payload)

    _validate(payload)

    assert payload == snapshot
