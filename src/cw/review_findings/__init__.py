"""Executor-neutral structured finding contract (#1237).

A schema + validation/dedup/aggregation library for code-review findings that
is neutral to the executor that produced them (Claude, Codex, or any future
backend). The model group here is the typed home for what `.claude/commands/
review.md` describes in prose: per-reviewer findings, the ESCALATIONS protocol
(§8 of that file, lines ~246-253), evidence-quote validation against the diff
(Step 5, line ~294), dedup/consolidation across reviewers (Step 5.9), and the
`.claude/review-verdict.json` artifact (#1108).

Ticket lineage: #1237 (this contract) builds on the sentinel contract in
`docs/headless-contract.md` / `auto_dev_result.py` (#1194) and feeds the
verdict artifact consumed downstream (#1108). Nothing here is wired to an
executor or CLI call site yet — this is the greenfield library the adapters
will build on.

This package was split out of a single ``review_findings.py`` module (#1818);
the public import surface (``from cw.review_findings import X``) is preserved
here via re-exports. Submodules:

- ``_models`` — type aliases, model-only constants, and every Pydantic/
  TypedDict class the contract exposes. Imports from no sibling.
- ``_validation`` — mechanical file/line-anchor resolution, evidence-quote
  matching, escalation stripping, and ``validate_reviewer_document``.
- ``_dedup`` — cross-reviewer dedup into ``AcceptedFinding`` groups and the
  ``Review`` count aggregation.
- ``_consolidate`` — ``consolidate_verdict`` orchestration and the atomic
  ``ReviewVerdict`` artifact writer.

Import-cycle note (#1818): ``cw.review_finding_dispositions`` documents a
load-bearing cycle broken only by that module refusing any module-scope ``cw``
import, and the flat module's first two ``cw``-scoped imports — ``cw.atomic``
then ``cw.auto_dev_result`` — are what that discipline was verified against.
The split preserves that order rather than the submodule order: isort sorts
the re-export block alphabetically, so ``_consolidate`` is imported first, and
its own first two imports are ``cw.atomic`` followed (via ``_dedup``) by
``cw.auto_dev_result``. ``tests/test_review_finding_dispositions.py``'s
cold-interpreter import-order matrix is the regression guard.
"""

from __future__ import annotations

from cw.review_findings._consolidate import (
    _select_rejected_must_fix,
    consolidate_verdict,
    write_review_verdict,
)
from cw.review_findings._dedup import (
    _dedup_key,
    dedupe_findings,
    derive_review_counts,
)
from cw.review_findings._models import (
    FINGERPRINT_VERSION,
    AcceptedFinding,
    AgentSpecSource,
    AgentSpecStatus,
    CapabilityMode,
    CapturedDiff,
    Confidence,
    DebtRecord,
    Disposition,
    EscalationMetadata,
    EscalationStripReason,
    Finding,
    RejectedFinding,
    RejectedFindingReason,
    ReviewerFindingsDocument,
    ReviewerHealthStatus,
    ReviewerRunFailure,
    ReviewerRunMetrics,
    ReviewerRunRecord,
    ReviewVerdict,
    Severity,
    StrippedEscalation,
    TrackingDisposition,
)
from cw.review_findings._validation import (
    _LINE_ANCHOR_TOLERANCE,
    _VALID_SEVERITIES,
    _anchor_in_enclosing_def,
    _classify_finding,
    _enclosing_def_span,
    _evidence_in_claimed_lines,
    _line_reference_valid,
    _reconcile_evidence_window,
    validate_reviewer_document,
)

__all__ = [
    "FINGERPRINT_VERSION",
    "_LINE_ANCHOR_TOLERANCE",
    "_VALID_SEVERITIES",
    "AcceptedFinding",
    "AgentSpecSource",
    "AgentSpecStatus",
    "CapabilityMode",
    "CapturedDiff",
    "Confidence",
    "DebtRecord",
    "Disposition",
    "EscalationMetadata",
    "EscalationStripReason",
    "Finding",
    "RejectedFinding",
    "RejectedFindingReason",
    "ReviewVerdict",
    "ReviewerFindingsDocument",
    "ReviewerHealthStatus",
    "ReviewerRunFailure",
    "ReviewerRunMetrics",
    "ReviewerRunRecord",
    "Severity",
    "StrippedEscalation",
    "TrackingDisposition",
    "_anchor_in_enclosing_def",
    "_classify_finding",
    "_dedup_key",
    "_enclosing_def_span",
    "_evidence_in_claimed_lines",
    "_line_reference_valid",
    "_reconcile_evidence_window",
    "_select_rejected_must_fix",
    "consolidate_verdict",
    "dedupe_findings",
    "derive_review_counts",
    "validate_reviewer_document",
    "write_review_verdict",
]
