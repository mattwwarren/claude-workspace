"""Re-export completeness guard for the ``cw.review_findings`` package (#1818).

The ``review_findings.py`` -> ``cw/review_findings/`` package split must
preserve every ``from cw.review_findings import X`` call site unchanged. This
mirrors ``tests/test_models.py``'s ``TestPackageExportCompleteness`` (written
for the #1320 ``cw.models`` split): ``__all__`` is asserted against an
exhaustive hardcoded set, deliberately NOT re-derived from the package, so a
dropped or renamed export is a falsifiable failure rather than a tautology. A
deliberate addition updates this set in the same commit.
"""

from __future__ import annotations

import cw.review_findings as rf

# The complete re-export surface: 9 type aliases, FINGERPRINT_VERSION, the 13
# model classes, the 5 public functions, and the 10 private names that existing
# cross-module call sites (``cw.codex_fix_loop_convergence``,
# ``cw.review_adjudication``, ``tests/test_review_findings.py``) import
# directly.
EXPECTED_EXPORTS = {
    # Type aliases
    "AgentSpecSource",
    "CapabilityMode",
    "Confidence",
    "Disposition",
    "EscalationStripReason",
    "RejectedFindingReason",
    "ReviewerHealthStatus",
    "Severity",
    "TrackingDisposition",
    # Module-level constant
    "FINGERPRINT_VERSION",
    # Models
    "AcceptedFinding",
    "AgentSpecStatus",
    "CapturedDiff",
    "DebtRecord",
    "EscalationMetadata",
    "Finding",
    "RejectedFinding",
    "ReviewVerdict",
    "ReviewerFindingsDocument",
    "ReviewerRunFailure",
    "ReviewerRunMetrics",
    "ReviewerRunRecord",
    "StrippedEscalation",
    # Public functions
    "consolidate_verdict",
    "dedupe_findings",
    "derive_review_counts",
    "validate_reviewer_document",
    "write_review_verdict",
    # Private names with confirmed cross-module import sites
    "_LINE_ANCHOR_TOLERANCE",
    "_VALID_SEVERITIES",
    "_anchor_in_enclosing_def",
    "_classify_finding",
    "_dedup_key",
    "_diff_pair_rescue",
    "_enclosing_def_span",
    "_evidence_diff_pair",
    "_evidence_in_claimed_lines",
    "_line_reference_valid",
    "_normalize_diff_text",
    "_normalize_unicode_punctuation",
    "_reconcile_evidence_window",
    "_select_rejected_must_fix",
    "_strip_diff_markers",
}


class TestPackageExportCompleteness:
    """Guards that ``cw.review_findings`` re-exports its full import surface."""

    def test_all_matches_full_surface(self) -> None:
        assert set(rf.__all__) == EXPECTED_EXPORTS

    def test_every_exported_name_is_bound(self) -> None:
        """A typo'd re-export must fail here, not at a downstream import site."""
        missing = [name for name in rf.__all__ if not hasattr(rf, name)]
        assert missing == []
