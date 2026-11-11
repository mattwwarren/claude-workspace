"""The codex engine aggregation turn's output contract (RFC 0018 A4, #3216).

On a codex review lane the engine's last ``codex exec`` turn aggregates the
role documents into one summary. The model emits only the judgement fields
(:class:`EngineAggregationOutput`); cw fills ``schema_version``, ``head_sha``
and ``counts`` (:func:`assemble_summary`), writes the summary file itself, and
validates it through the unchanged ``load_review_engine_summary``.

Light module: it imports only ``cw.models.review_engine_summary``,
``cw.openai_strict_schema`` and :mod:`cw.review_engine.constants`. It is not
re-exported from ``cw.review_engine``; import it by submodule path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from cw.models.review_engine_summary import (
    MAX_ENGINE_ACCEPTANCE_ROWS,
    MAX_ENGINE_FINDINGS,
    MAX_ENGINE_GATE_SIGNALS,
    MAX_ENGINE_RISK_NOTES,
    REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
    AcceptanceRow,
    EngineCounts,
    EngineCoverage,
    EngineFinding,
    EngineRisk,
    EngineVerdict,
    GateSignal,
    _RiskNote,
)
from cw.openai_strict_schema import to_openai_strict_schema
from cw.review_engine.constants import ENGINE_AGGREGATOR_PREAMBLE

if TYPE_CHECKING:
    from collections.abc import Sequence


class EngineAggregationOutput(BaseModel):
    """What the aggregation turn emits: the summary minus cw's three fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: EngineVerdict = Field(
        description="PASS, or FAIL; FAIL if and only if a MUST_FIX or SHOULD_FIX "
        "finding exists."
    )
    coverage: EngineCoverage = Field(
        description="COMPLETE only when every role document reviewed the change."
    )
    risk: EngineRisk = Field(description="The overall risk rating.")
    risk_notes: list[_RiskNote] = Field(
        max_length=MAX_ENGINE_RISK_NOTES,
        description="Short notes behind the risk rating; may be empty.",
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


def assemble_summary(output: EngineAggregationOutput, head_sha: str) -> dict[str, Any]:
    """The summary payload: *output* plus ``schema_version``, ``head_sha``, counts.

    Returned as a JSON-ready mapping so the caller writes it and the unchanged
    ``load_review_engine_summary`` validates it, cross-field rules included.
    """
    body = output.model_dump(mode="json")
    counts = EngineCounts.tally(output.findings)
    return {
        "schema_version": REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
        "head_sha": head_sha,
        "verdict": body["verdict"],
        "coverage": body["coverage"],
        "risk": body["risk"],
        "risk_notes": body["risk_notes"],
        "counts": counts.model_dump(mode="json"),
        "findings": body["findings"],
        "acceptance": body["acceptance"],
        "gate_signals": body["gate_signals"],
    }


def engine_aggregation_schema() -> dict[str, Any]:
    """The strict ``--output-schema`` for the aggregation turn."""
    return to_openai_strict_schema(EngineAggregationOutput.model_json_schema())


def render_aggregation_envelope(
    *,
    head_sha: str,
    aggregation_prompt: str,
    role_documents: Sequence[tuple[str, str]],
) -> str:
    """The aggregation turn's stdin, role documents in manifest order.

    *role_documents* pairs each role id with its findings document JSON.
    """
    parts = [
        ENGINE_AGGREGATOR_PREAMBLE,
        f"Reviewed head: {head_sha}",
        f"## Aggregation Instructions\n{aggregation_prompt.strip()}",
    ]
    for role_id, document in role_documents:
        parts.append(f"## Role Document: {role_id}\n```json\n{document}\n```")
    return "\n\n".join(parts) + "\n"
