"""Claude-native review adjudication seam (#1805).

``cw review consolidate`` leaves every surviving :class:`AcceptedFinding` at
``disposition="fixed"`` — an optimistic default, correct at the moment it is
written because nothing has been adjudicated yet. The Codex fix-loop adapter
overwrites it from its own re-review. The Claude-native ``/auto-dev-review``
pipeline had no equivalent overwrite: Checkpoint 3a's FIX / REJECT / DEFER
adjudication happened entirely in LLM prose, and ``.cw/deferred-findings.md``
was hand-authored from that same prose — two independently typed records of
one judgment, only one of which was accurate.

This package is that missing seam. :func:`apply_adjudication` stamps the
dispositions from a structured :class:`Adjudication` list and recomputes the
gate-feeding fields from the stamped result;
:func:`render_deferred_findings_md` renders the *same* list into the
``.cw/deferred-findings.md`` artifact, so the two records cannot disagree.
:func:`verify_fixed_dispositions` then downgrades any ``"fixed"`` claim the
fix-cycle diff does not substantiate. The judgment step itself is untouched —
only its serialization becomes mechanical.

**Why this is a Claude-native-only seam, and why it must not be unified with
the Codex path (#1805 R2 — the durable principle lives here rather than in a
new ADR).** :func:`cw.codex_fix_loop._survivors_only_verdict` also stamps
``disposition`` and recomputes ``blocking``, but its ``"deferred"`` means the
opposite of this package's: there it means "the fix loop capped out, this
MUST_FIX is still genuinely unresolved" (so ``blocking`` must stay ``True``
for a deferred survivor, which is exactly why that function computes
``blocking`` from the open-finding set rather than from dispositions). Here
``"deferred"`` means "the coordinating session deliberately decided this may
ship un-fixed" — so a deferred finding correctly stops blocking. The two
shapes must NOT be unified by generalizing one to cover the other. If a
second consumer ever needs *this* seam (not merely the ``Disposition`` /
:class:`AcceptedFinding` types, which are shared already), that is the trigger
to write an ADR; until then this docstring is the record of why they differ.

**The one deliberate exception (#1814).** :class:`VoidedFinding` /
:func:`apply_voided_suppression` and their helpers ARE shared by both
backends. That is not a breach of the rule above but the carve-out it already
names: they reuse only the already-declared-shared ``Disposition`` /
:class:`AcceptedFinding` types, never :func:`apply_adjudication` itself and
never the ``"defer"`` outcome whose two meanings are the actual reason the
seams must stay apart. Suppression produces exactly one outcome — ``"reject"``
— whose meaning is identical on both paths, which is why one implementation
can serve both. See ADR-0015 for the durable invariant.

Public surface: :class:`Adjudication`, :data:`AdjudicationOutcome`,
:func:`apply_adjudication`, :func:`matched_adjudications`,
:func:`verify_fixed_dispositions`, :func:`render_deferred_findings_md`,
:func:`parse_deferred_findings_md`, :func:`merge_deferred_adjudications`,
:data:`REJECTED_ENTRY_SEVERITY`,
:class:`VoidedFinding`, :func:`find_voided_matches`,
:func:`apply_voided_suppression`, :func:`render_voided_findings_block`,
:func:`parse_voided_findings_block`.

This package was split out of a single ``review_adjudication.py`` module
(#2011); the public import surface (``from cw.review_adjudication import X``)
is preserved here via re-exports. The four paragraphs above are why the split
runs along these seams rather than any other: the module bundled four
concerns that cross-reference each other only through the shared
``Adjudication``/``AcceptedFinding`` types, never through logic. Submodules:

- ``_models`` — :class:`Adjudication`, :class:`VoidedFinding`, the
  ``AdjudicationOutcome`` vocabulary and its disposition mapping. Imports from
  no sibling.
- ``_match`` — the #1805 core: location-key matching of adjudication entries
  against accepted findings, disposition stamping, and the gate-feeding
  recompute. Imports ``_models``.
- ``_voided`` — the #1814 carve-out named above: content-anchored void
  matching, suppression (with its inline ``review.finding_voided`` event), and
  the ``VOIDED-REVIEW-FINDINGS`` JSON sentinel's render/parse. Imports
  ``_models``.
- ``_verify`` — the #2000/#2007 fix-claim verification. A clean leaf: it
  touches neither :class:`Adjudication` nor :class:`VoidedFinding`, so it
  imports no sibling.
- ``_deferred_md`` — the ``.cw/deferred-findings.md`` artifact's
  render/parse/merge, including :data:`REJECTED_ENTRY_SEVERITY` and the
  ``DEFERRED-REVIEW-FINDINGS`` markdown sentinel. Imports ``_models``.

``_fix_is_substantiated`` is re-exported despite being private because
``tests/test_review_adjudication.py`` imports it directly — the same economy
:mod:`cw.review_findings` applies to ``_select_rejected_must_fix``. Every
other private helper stays inside its submodule;
``tests/test_review_adjudication_reexports.py`` is the falsifiable guard on
that surface.
"""

from __future__ import annotations

from cw.review_adjudication._deferred_md import (
    REJECTED_ENTRY_SEVERITY,
    merge_deferred_adjudications,
    parse_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_adjudication._match import (
    NO_ENTRY_DETAIL,
    apply_adjudication,
    matched_adjudications,
)
from cw.review_adjudication._models import (
    Adjudication,
    AdjudicationOutcome,
    VoidedFinding,
)
from cw.review_adjudication._verify import (
    _fix_is_substantiated,
    verify_fixed_dispositions,
)
from cw.review_adjudication._voided import (
    apply_voided_suppression,
    find_voided_matches,
    parse_voided_findings_block,
    render_voided_findings_block,
)

__all__ = [
    "NO_ENTRY_DETAIL",
    "REJECTED_ENTRY_SEVERITY",
    "Adjudication",
    "AdjudicationOutcome",
    "VoidedFinding",
    "_fix_is_substantiated",
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
]
