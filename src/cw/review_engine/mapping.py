"""Engine summary → reviewer finding mapping (RFC 0017 R10).

Pure: no I/O. :func:`engine_document` builds the one ``ReviewerFindingsDocument``
an engine round records under :data:`ENGINE_ROLE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_engine.constants import (
    ENGINE_DEFAULT_CONSEQUENCE,
    ENGINE_DEFAULT_SUGGESTED_FIX,
    ENGINE_ROLE,
)
from cw.review_findings import Finding, ReviewerFindingsDocument

_NO_FILE = "N/A"

if TYPE_CHECKING:
    from cw.models.review_engine_summary import EngineFinding, ReviewEngineSummary


def engine_finding_to_finding(finding: EngineFinding) -> Finding:
    """Map one engine finding onto a reviewer :class:`Finding`.

    Confidence is always HIGH: the engine's own gate decides, and cw does not
    re-score it. A missing file becomes ``N/A`` with ``no_diff_anchor`` set.
    A line becomes both ``line_start`` and ``line_end``.
    """
    has_file = finding.file is not None
    return Finding(
        severity=finding.severity,
        file=finding.file if finding.file is not None else _NO_FILE,
        line_start=finding.line,
        line_end=finding.line,
        summary=finding.summary,
        consequence=finding.consequence or ENGINE_DEFAULT_CONSEQUENCE,
        suggested_fix=finding.suggested_fix or ENGINE_DEFAULT_SUGGESTED_FIX,
        evidence=finding.evidence,
        confidence="HIGH",
        no_diff_anchor=not has_file,
        fingerprint=finding.fingerprint,
        rule_id=finding.rule_id,
        source="engine",
    )


def _detail(summary: ReviewEngineSummary, skill: str) -> str:
    """Verdict, coverage, risk and counts. Never finding or skill text."""
    gate_signals = len(summary.gate_signals)
    return (
        f"repo review engine ({skill}) verdict={summary.verdict} "
        f"coverage={summary.coverage} risk={summary.risk} "
        f"findings={len(summary.findings)} "
        f"acceptance_rows={len(summary.acceptance)} "
        f"gate_signals={gate_signals}"
    )


def engine_document(
    summary: ReviewEngineSummary, skill: str
) -> ReviewerFindingsDocument:
    """The single ``ok`` document an engine round records for its role."""
    return ReviewerFindingsDocument(
        reviewer_role=ENGINE_ROLE,
        status="ok",
        detail=_detail(summary, skill),
        findings=[engine_finding_to_finding(f) for f in summary.findings],
    )
