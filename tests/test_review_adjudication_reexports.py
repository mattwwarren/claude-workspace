"""Re-export completeness guard for ``cw.review_adjudication`` (#2011).

The ``review_adjudication.py`` -> ``cw/review_adjudication/`` package split must
preserve every ``from cw.review_adjudication import X`` call site unchanged.
This mirrors ``tests/test_review_findings_reexports.py`` (written for the #1818
split of the neighbouring module): ``__all__`` is asserted against an exhaustive
hardcoded set, deliberately NOT re-derived from the package, so a dropped or
renamed export is a falsifiable failure rather than a tautology. A deliberate
addition updates this set in the same commit.
"""

from __future__ import annotations

import cw.review_adjudication as ra

# The complete re-export surface: the module docstring's documented public
# names, plus ``_fix_is_substantiated`` — the one private name with a confirmed
# cross-module import site (``tests/test_review_adjudication.py``).
EXPECTED_EXPORTS = {
    # Type alias
    "AdjudicationOutcome",
    # Module-level constants
    "NO_ENTRY_DETAIL",
    "REJECTED_ENTRY_SEVERITY",
    # Models
    "Adjudication",
    "VoidedFinding",
    # Public functions
    "apply_adjudication",
    "apply_voided_suppression",
    "find_voided_matches",
    "matched_adjudications",
    "merge_deferred_adjudications",
    "parse_deferred_findings_md",
    "parse_voided_findings_block",
    "render_deferred_findings_md",
    "render_voided_findings_block",
    "verify_fixed_dispositions",
    # Private names with confirmed cross-module import sites
    "_fix_is_substantiated",
}


class TestPackageExportCompleteness:
    """Guards that ``cw.review_adjudication`` re-exports its full surface."""

    def test_all_matches_full_surface(self) -> None:
        assert set(ra.__all__) == EXPECTED_EXPORTS

    def test_every_exported_name_is_bound(self) -> None:
        """A typo'd re-export must fail here, not at a downstream import site."""
        missing = [name for name in ra.__all__ if not hasattr(ra, name)]
        assert missing == []
