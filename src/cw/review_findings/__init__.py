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
- ``_text_match`` — pure text-normalization and window-reconciliation
  primitives (#1715/#1976/#1792). Stdlib only; imports from no sibling.
- ``_reanchor`` — #2007's content-based rescue for a line citation that
  drifted past every tolerance-bounded gate. Imports ``_text_match`` only.
- ``_anchor`` — diff-anchoring geometry: which files/lines the diff touches,
  near-miss anchor resolution (#1715), added-line vs hunk-window resolution
  (#1738), the enclosing-def fallback (#1743), and the diff-pair rescue
  (#1976). Answers where a citation lands, never what to do about it.
- ``_classify`` — the verdict layer over ``_anchor``: the fixed
  classification check order, the persisted-anchor repair, and the
  diagnosable ``RejectedFinding.detail`` messages. Imports ``_anchor``.
- ``_document`` — document-level entry points: ``validate_reviewer_document``
  (accepted/rejected/stripped partition, escalation stripping) and #2029's
  tolerant ``parse_reviewer_document``. Imports ``_classify``.
- ``_dedup`` — cross-reviewer dedup into ``AcceptedFinding`` groups and the
  ``Review`` count aggregation.
- ``_consolidate`` — ``consolidate_verdict`` orchestration and the atomic
  ``ReviewVerdict`` artifact writer.

#2000 added three counters to ``ReviewVerdict`` — ``rejected_count``,
``rejected_count_by_severity`` (both stamped by ``consolidate_verdict``, and
mirrored onto the nested ``Review`` so they reach the terminal
``AUTO_DEV_RESULT`` sentinel) and ``downgraded_disposition_count`` (stamped by
``cw.review_adjudication``). They need no new export: the existing
``ReviewVerdict`` re-export already carries them. Their counting helper,
``_consolidate._count_rejected_by_severity``, stays package-private —
``_select_rejected_must_fix`` is exported only because its #1714 selection rule
is directly test-asserted, which is not true here.

#2029 follows the same economy: the new ``"schema_invalid"`` rejection reason,
``ReviewerRunFailure``'s discard tally, and ``ReviewVerdict``'s
``run_failures_with_should_fix_discards`` all ride the existing type re-exports
and need no new names. ``parse_reviewer_document`` is added — the tolerant
JSON→model boundary both executor paths call in place of
``ReviewerFindingsDocument.model_validate``, so it has to be public.
``_best_effort_discarded_tally`` is also re-exported: ``cw.codex_review._roles``
needs it directly, mirroring ``_select_rejected_must_fix``'s precedent above.
Its remaining helpers (``_raw_finding_payload``,
``_select_run_failures_with_discards``) stay package-private.

Import-cycle note (#1818, revised #2054): ``cw.review_finding_dispositions``
documents a load-bearing cycle broken only by that module refusing any
module-scope ``cw`` import. isort sorts the re-export block alphabetically, so
``_anchor`` is imported first — but it and its only dependency ``_text_match``
are stdlib-only leaves, so the first ``cw``-scoped import from OUTSIDE this
package now arrives one block later, via ``_classify`` → ``_models`` →
``cw.auto_dev_result``; ``cw.atomic`` follows much later via ``_consolidate``.

That is a genuine reversal of the ``cw.atomic``-then-``cw.auto_dev_result``
order this package presented before the split, and it is deliberately NOT
preserved: pinning an import order would be pinning a symptom. The invariant
that actually matters is that ``cw.review_finding_dispositions`` holds no
module-scope ``cw`` import, which makes the cycle unclosable in EVERY order
rather than in the one order we happened to ship.
``tests/test_review_finding_dispositions.py::test_module_imports_cleanly_whichever_module_loads_first``
is the regression guard: it imports each participant first in a cold
interpreter, so an order-dependent cycle fails there regardless of which
submodule this block happens to load first.
"""

from __future__ import annotations

from cw.review_findings._anchor import (
    _anchor_in_enclosing_def,
    _diff_pair_rescue,
    _enclosing_def_span,
    _evidence_in_claimed_lines,
    _line_reference_valid,
)
from cw.review_findings._classify import (
    _VALID_SEVERITIES,
    _classify_finding,
)
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
from cw.review_findings._document import (
    _best_effort_discarded_tally,
    _rescue_findings,
    parse_reviewer_document,
    validate_reviewer_document,
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
from cw.review_findings._reanchor import (
    _cited_lines_on_disk,
    _content_rescue_anchor,
    _evidence_removed_in_fix_diff,
    _line_exceeds_file_length,
)
from cw.review_findings._text_match import (
    _LINE_ANCHOR_TOLERANCE,
    _evidence_diff_pair,
    _normalize_diff_text,
    _normalize_unicode_punctuation,
    _reconcile_evidence_window,
    _strip_diff_markers,
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
    "_best_effort_discarded_tally",
    "_cited_lines_on_disk",
    "_classify_finding",
    "_content_rescue_anchor",
    "_dedup_key",
    "_diff_pair_rescue",
    "_enclosing_def_span",
    "_evidence_diff_pair",
    "_evidence_in_claimed_lines",
    "_evidence_removed_in_fix_diff",
    "_line_exceeds_file_length",
    "_line_reference_valid",
    "_normalize_diff_text",
    "_normalize_unicode_punctuation",
    "_reconcile_evidence_window",
    "_rescue_findings",
    "_select_rejected_must_fix",
    "_strip_diff_markers",
    "consolidate_verdict",
    "dedupe_findings",
    "derive_review_counts",
    "parse_reviewer_document",
    "validate_reviewer_document",
    "write_review_verdict",
]
