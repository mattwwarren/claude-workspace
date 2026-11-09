"""``ReviewEngineSummary``: the cw-owned contract for a review engine's summary.

A repo's own review skill is the round engine (RFC 0017, #3036). cw hands it an
output path; the engine writes one JSON summary there, and cw validates that
summary strictly and fails closed. cw never parses an engine's markdown report.

Output-only from cw's side: cw reads the summary and never writes or persists
one, so every model is ``extra="forbid"`` and ``strict=True`` (no ``true`` or
``"1"`` coerced to an int). ``cw schema show review-engine-summary`` prints its
JSON schema, and ``docs/headless-contract.md`` section 12 is the prose contract.

Compatibility: ``REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION`` is checked exactly, with
no look-ahead. Any field change bumps it and adds a row to section 12's version
history.

Fingerprints are opaque and engine-owned: cw requires them unique within one
summary but never computes or verifies them, and their stability across rounds
is the engine's contract, not cw's.

The four cross-field rules raise a ``PydanticCustomError`` whose type is in
``INCONSISTENT_ERROR_TYPES`` and whose ``ctx["field_path"]`` names the field;
``cw.review_engine_summary_io`` maps those to the ``inconsistent`` rejection.
Every other error is a single-field malformation (``schema_invalid``).

Pure pydantic with no ``cw`` import. Imported by submodule path and not
re-exported from ``cw.models``, as ``cw.models.review_history`` is not.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION = 1

#: Raw-byte cap, checked before decoding (reason ``oversized``).
MAX_ENGINE_SUMMARY_BYTES = 1_048_576

# Count caps (reason ``schema_invalid``).
MAX_ENGINE_FINDINGS = 500
MAX_ENGINE_ACCEPTANCE_ROWS = 200
MAX_ENGINE_GATE_SIGNALS = 64
MAX_ENGINE_RISK_NOTES = 32

# String caps (reason ``schema_invalid``).
MAX_ENGINE_RISK_NOTE_LENGTH = 1_000
MAX_ENGINE_FINDING_SUMMARY_LENGTH = 2_000
MAX_ENGINE_EVIDENCE_LENGTH = 8_000
MAX_ENGINE_CONSEQUENCE_LENGTH = 2_000
MAX_ENGINE_SUGGESTED_FIX_LENGTH = 4_000
MAX_ENGINE_ACCEPTANCE_ID_LENGTH = 64
MAX_ENGINE_ACCEPTANCE_EVIDENCE_LENGTH = 2_000
MAX_ENGINE_GATE_NAME_LENGTH = 64
MAX_ENGINE_GATE_DETAIL_LENGTH = 2_000
MAX_ENGINE_FILE_LENGTH = 512
MIN_ENGINE_FINGERPRINT_LENGTH = 8
MAX_ENGINE_FINGERPRINT_LENGTH = 128
MAX_ENGINE_RULE_ID_LENGTH = 128

ENGINE_FINGERPRINT_CHARSET_RE = r"^[A-Za-z0-9._:-]+$"
ENGINE_RULE_ID_RE = r"^[a-z0-9_]+(\.[a-z0-9_-]+)*$"
#: The ``ReviewFeedback.reviewed_sha`` rule (a drift test pins the equality).
ENGINE_HEAD_SHA_RE = r"^[0-9a-f]{40}$"

#: Reserved rule-id prefixes. Exported for I1's non-deferrable rule (D-6); no
#: cw code consumes them in S1.
COVERAGE_RULE_PREFIX = "coverage."
ACCEPTANCE_RULE_PREFIX = "acceptance."

#: The cross-field rules, keyed by error type. ``str.format`` templates: they
#: are rendered before the error is built, because pydantic-core substitutes
#: only plain ``{key}`` placeholders and would leave ``{fingerprint!r}`` as is.
_DUPLICATE_FINGERPRINT: Final = "engine_duplicate_fingerprint"
_COUNTS_MISMATCH: Final = "engine_counts_mismatch"
_VERDICT_GATING_CONFLICT: Final = "engine_pass_with_gating_finding"
_LINE_WITHOUT_FILE: Final = "engine_line_without_file"
INCONSISTENT_MESSAGES: Final[dict[str, str]] = {
    _DUPLICATE_FINGERPRINT: "duplicate fingerprint {fingerprint!r}",
    _COUNTS_MISMATCH: "counts {declared} do not match findings tally {actual}",
    _VERDICT_GATING_CONFLICT: (
        "verdict PASS with {count} MUST_FIX/SHOULD_FIX finding(s)"
    ),
    _LINE_WITHOUT_FILE: "line {line} given without file",
}
INCONSISTENT_ERROR_TYPES: Final = frozenset(INCONSISTENT_MESSAGES)

# Declared locally rather than imported from ``cw.review_findings``, which would
# close an import cycle through ``cw.auto_dev_result``. A drift test asserts
# ``get_args(EngineSeverity) == get_args(cw.review_findings._models.Severity)``.
EngineSeverity = Literal["MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"]
EngineVerdict = Literal["PASS", "FAIL"]
#: Only ``COMPLETE`` counts as a reviewed round (D-3); S1 only exposes it.
EngineCoverage = Literal["COMPLETE", "PARTIAL", "FAILED"]
EngineRisk = Literal["LOW", "MEDIUM", "HIGH"]
AcceptanceStatus = Literal["PASS", "FAIL", "NOT_APPLICABLE"]
GateSignalStatus = Literal["PASS", "FAIL", "WARN"]

_GATING_SEVERITIES: Final = frozenset({"MUST_FIX", "SHOULD_FIX"})
_NOT_APPLICABLE_FILE = "n/a"


def _inconsistent(
    error_type: str, field_path: str, **values: object
) -> PydanticCustomError:
    """One cross-field error, its D2 template rendered with *values*."""
    message = INCONSISTENT_MESSAGES[error_type].format(**values)
    return PydanticCustomError(error_type, message, {"field_path": field_path})


def _non_blank_text(value: str) -> str:
    """Reject an empty or whitespace-only string."""
    if not value.strip():
        msg = "must not be blank"
        raise ValueError(msg)
    return value


def _non_blank(value: str | None) -> str | None:
    """``_non_blank_text`` for a nullable field; ``None`` passes."""
    return None if value is None else _non_blank_text(value)


_RiskNote = Annotated[
    str,
    Field(max_length=MAX_ENGINE_RISK_NOTE_LENGTH),
    AfterValidator(_non_blank_text),
]


class EngineCounts(BaseModel):
    """How many findings the engine reports at each severity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    must_fix: int = Field(ge=0, description="Number of MUST_FIX findings.")
    should_fix: int = Field(ge=0, description="Number of SHOULD_FIX findings.")
    debt: int = Field(ge=0, description="Number of DEBT findings.")
    nit: int = Field(ge=0, description="Number of NIT findings.")
    principle: int = Field(ge=0, description="Number of PRINCIPLE findings.")

    @classmethod
    def tally(cls, findings: list[EngineFinding]) -> EngineCounts:
        """The counts *findings* actually carry, by severity."""
        counts = dict.fromkeys(cls.model_fields, 0)
        for finding in findings:
            counts[finding.severity.lower()] += 1
        return cls.model_validate(counts)


def _counts_text(counts: EngineCounts) -> str:
    """``must_fix=1 should_fix=0 ...`` for the counts-mismatch message."""
    return " ".join(f"{name}={value}" for name, value in counts.model_dump().items())


class EngineFinding(BaseModel):
    """One finding the engine reports."""

    model_config = ConfigDict(extra="forbid", strict=True)

    fingerprint: str = Field(
        min_length=MIN_ENGINE_FINGERPRINT_LENGTH,
        max_length=MAX_ENGINE_FINGERPRINT_LENGTH,
        pattern=ENGINE_FINGERPRINT_CHARSET_RE,
        description=(
            "Opaque engine-owned identity, unique within the summary and stable "
            "across rounds by the engine's contract."
        ),
    )
    severity: EngineSeverity = Field(description="The finding's severity.")
    rule_id: str = Field(
        max_length=MAX_ENGINE_RULE_ID_LENGTH,
        pattern=ENGINE_RULE_ID_RE,
        description="Dotted lowercase rule identifier, such as `style.naming`.",
    )
    file: str | None = Field(
        max_length=MAX_ENGINE_FILE_LENGTH,
        description="Repo-relative file the finding is about, or null for none.",
    )
    line: int | None = Field(
        ge=1, description="1-based line in `file`, or null; requires a `file`."
    )
    summary: str = Field(
        max_length=MAX_ENGINE_FINDING_SUMMARY_LENGTH,
        description="One-paragraph statement of the problem.",
    )
    evidence: str = Field(
        max_length=MAX_ENGINE_EVIDENCE_LENGTH,
        description="What the engine saw that supports the finding.",
    )
    consequence: str | None = Field(
        default=None,
        max_length=MAX_ENGINE_CONSEQUENCE_LENGTH,
        description="What goes wrong if the finding is left in place, if stated.",
    )
    suggested_fix: str | None = Field(
        default=None,
        max_length=MAX_ENGINE_SUGGESTED_FIX_LENGTH,
        description="How the engine suggests fixing it, if stated.",
    )

    @field_validator("file")
    @classmethod
    def _check_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            msg = "file must not be blank; use null for no file"
            raise ValueError(msg)
        if stripped.casefold() == _NOT_APPLICABLE_FILE:
            msg = "file must not be 'N/A'; use null for no file"
            raise ValueError(msg)
        return value

    @field_validator("summary", "evidence", "consequence", "suggested_fix")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        return _non_blank(value)

    @model_validator(mode="after")
    def _line_requires_file(self) -> EngineFinding:
        if self.line is not None and self.file is None:
            raise _inconsistent(_LINE_WITHOUT_FILE, "line", line=self.line)
        return self


class AcceptanceRow(BaseModel):
    """One acceptance criterion and whether the change meets it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(
        max_length=MAX_ENGINE_ACCEPTANCE_ID_LENGTH,
        description="The criterion's identifier, such as `ac1`.",
    )
    status: AcceptanceStatus = Field(description="Whether the criterion is met.")
    evidence: str | None = Field(
        max_length=MAX_ENGINE_ACCEPTANCE_EVIDENCE_LENGTH,
        description="Why the engine reached that status, or null.",
    )

    @field_validator("id", "evidence")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        return _non_blank(value)


class GateSignal(BaseModel):
    """One quality-gate signal the engine observed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(
        max_length=MAX_ENGINE_GATE_NAME_LENGTH,
        description="The gate's name, such as `ruff`.",
    )
    status: GateSignalStatus = Field(description="The gate's outcome.")
    detail: str | None = Field(
        max_length=MAX_ENGINE_GATE_DETAIL_LENGTH,
        description="Short detail on the outcome, or null.",
    )

    @field_validator("name", "detail")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        return _non_blank(value)


class ReviewEngineSummary(BaseModel):
    """The JSON summary a review engine writes for one review round."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(
        description="Summary schema version; must equal the version cw supports."
    )
    head_sha: str = Field(
        pattern=ENGINE_HEAD_SHA_RE,
        description="The 40-character lowercase commit sha the engine reviewed.",
    )
    verdict: EngineVerdict = Field(
        description="PASS, or FAIL; PASS allows no MUST_FIX or SHOULD_FIX finding."
    )
    coverage: EngineCoverage = Field(
        description="Whether the engine reviewed the whole change."
    )
    risk: EngineRisk = Field(description="The engine's overall risk rating.")
    risk_notes: list[_RiskNote] = Field(
        max_length=MAX_ENGINE_RISK_NOTES,
        description="Short notes behind the risk rating; may be empty.",
    )
    counts: EngineCounts = Field(
        description="Findings per severity; must equal the tally of `findings`."
    )
    findings: list[EngineFinding] = Field(
        max_length=MAX_ENGINE_FINDINGS, description="Every finding; may be empty."
    )
    acceptance: list[AcceptanceRow] = Field(
        max_length=MAX_ENGINE_ACCEPTANCE_ROWS,
        description="Acceptance-criterion rows; may be empty.",
    )
    gate_signals: list[GateSignal] = Field(
        max_length=MAX_ENGINE_GATE_SIGNALS,
        description="Quality-gate signals; may be empty.",
    )

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION:
            msg = (
                f"schema_version {value} is not supported "
                f"(expected {REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION})"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> ReviewEngineSummary:
        self._check_unique_fingerprints()
        self._check_counts()
        self._check_pass_verdict()
        return self

    def _check_unique_fingerprints(self) -> None:
        seen: set[str] = set()
        for index, finding in enumerate(self.findings):
            if finding.fingerprint in seen:
                raise _inconsistent(
                    _DUPLICATE_FINGERPRINT,
                    f"findings.{index}.fingerprint",
                    fingerprint=finding.fingerprint,
                )
            seen.add(finding.fingerprint)

    def _check_counts(self) -> None:
        actual = EngineCounts.tally(self.findings)
        if actual != self.counts:
            raise _inconsistent(
                _COUNTS_MISMATCH,
                "counts",
                declared=_counts_text(self.counts),
                actual=_counts_text(actual),
            )

    def _check_pass_verdict(self) -> None:
        if self.verdict != "PASS":
            return
        gating = sum(1 for f in self.findings if f.severity in _GATING_SEVERITIES)
        if gating:
            raise _inconsistent(_VERDICT_GATING_CONFLICT, "verdict", count=gating)
