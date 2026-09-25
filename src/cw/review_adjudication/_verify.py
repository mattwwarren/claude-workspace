"""Fix-claim verification against the fix-cycle diff (#2000/#2007).

:func:`verify_fixed_dispositions` downgrades every ``"fixed"`` disposition the
fix-cycle diff does not substantiate. Record-only by design: it never triggers
a new fix cycle and never recomputes ``blocking``/``must_fix``. A clean leaf —
it touches neither :class:`~cw.review_adjudication._models.Adjudication` nor
:class:`~cw.review_adjudication._models.VoidedFinding`.

Split out of the single ``review_adjudication.py`` module (#2011); import these
names from :mod:`cw.review_adjudication`, not from this private submodule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models.enums import OrchestratorEventType
from cw.review_findings import (
    _evidence_removed_in_fix_diff,
    _line_reference_valid,
)

if TYPE_CHECKING:
    from cw.review_findings import (
        AcceptedFinding,
        CapturedDiff,
        Finding,
        ReviewVerdict,
    )

_log = logging.getLogger(__name__)


def _fix_is_substantiated(finding: Finding, fix_diff: CapturedDiff) -> bool:
    """True iff *fix_diff* actually touches *finding*'s cited location.

    A file-level finding (both endpoints ``None``) needs only the file to
    appear in the diff. A line-anchored one goes through
    ``_line_reference_valid`` — the same tolerance-aware gate consolidation
    uses, so a fix landing a line or two off its cited anchor is not called a
    non-fix. ``worktree=None`` deliberately disables the #1743 enclosing-def
    fallback: "some line inside the enclosing function changed" is evidence a
    finding is *anchorable*, not evidence it was *fixed*.

    Past that tolerance, #2007 adds one further arm: a fix whose hunk shifted
    the cited code further than ±3 lines is substantiated when the fix-cycle
    diff genuinely REMOVED the finding's evidence. Bound to ``file_diffs``
    only — a removed line has no new-file line number, so it exists in no
    other substrate — and matched against ``-``-prefixed lines exclusively,
    never a plain substring of the hunk, which could not tell deleted code
    from untouched context elsewhere in the same file. Matching a removed line
    is a categorically stronger claim than the proximity the paragraph above
    refuses, which is why this arm is permitted and that one still is not.
    The arm is purely additive: when it finds nothing, the pre-#2007
    ``"dropped"`` downgrade stands untouched.
    """
    if finding.line_start is None and finding.line_end is None:
        return finding.file in fix_diff.files
    if _line_reference_valid(fix_diff, finding, worktree=None):
        return True
    if _evidence_removed_in_fix_diff(
        fix_diff.file_diffs, finding.file, finding.evidence
    ):
        _log.info(
            "auto-dev: rescued fixed disposition via fix-substantiation "
            "content rescue (file=%s, line=%d)",
            finding.file,
            finding.line_start if finding.line_start is not None else finding.line_end,
        )
        return True
    return False


def verify_fixed_dispositions(
    verdict: ReviewVerdict, fix_diff: CapturedDiff, *, ticket_id: str
) -> ReviewVerdict:
    """Downgrade every ``"fixed"`` claim *fix_diff* does not substantiate.

    A ``"fixed"`` disposition whose cited file/line the fix-cycle diff never
    touched becomes ``"dropped"`` with the reason in ``disposition_detail``.
    Any other disposition is passed through untouched — a deferred or rejected
    finding was never claimed to be fixed, so checking it against the fix diff
    could only produce a false downgrade.

    Record-only by design (#1805 R1): this never triggers a new fix cycle, and
    deliberately does NOT recompute ``blocking``/``must_fix`` the way
    :func:`apply_adjudication` does. The downgrade is a statement about the
    accuracy of the record, not a new gate; the caller
    (``auto-dev-review.md`` Step 3b) surfaces it in ``friction_highlights`` so
    a human sees it.

    ``downgraded_disposition_count`` (#2000) is stamped on the returned verdict
    with this call's own downgrade tally — the second, structured half of the
    same visibility fix the enriched WARNING below is the first half of. It is
    computed FRESH rather than added to the input verdict's value: Stage 3
    invokes this exactly once per pass (``auto-dev-review.md`` is the only call
    site), so the honest reading of the field is "this pass walked back N fixed
    claims", and accumulating would misreport a re-run as a worse pass.

    **Emits one ``review.fixed_disposition_downgraded`` event per downgrade,
    inline, correlated to** ``ticket_id`` (#2009) — the same "one event per
    mutation, not separable from it" contract as
    :func:`apply_voided_suppression`. Without it the downgrade's only records
    were the WARNING below and a bare count on the verdict artifact, neither
    of which survives into the event audit trail.
    """
    accepted: list[AcceptedFinding] = []
    downgraded = 0
    for af in verdict.accepted:
        if af.disposition != "fixed" or _fix_is_substantiated(af.finding, fix_diff):
            accepted.append(af)
            continue
        downgraded += 1
        # #2000: file/line alone did not identify WHICH finding's claim was
        # walked back -- an operator reading the log could not tell a NIT from
        # a MUST_FIX, or which reviewer had raised it.
        _log.warning(
            "auto-dev: downgraded 'fixed' disposition — fix-cycle diff does "
            "not touch cited location (file=%s, line=%s, reviewers=%s, "
            "severity=%s, title=%s)",
            af.finding.file,
            af.finding.line_start,
            ", ".join(af.reviewers),
            af.finding.severity,
            af.finding.summary,
        )
        downgraded_af = af.model_copy(
            update={
                "disposition": "dropped",
                "disposition_detail": (
                    "fixed disposition claimed but fix-cycle diff does not "
                    f"touch {af.finding.file}:{af.finding.line_start}"
                ),
            }
        )
        accepted.append(downgraded_af)
        record_event(
            OrchestratorEventType.REVIEW_FIXED_DISPOSITION_DOWNGRADED,
            payload={
                "file": af.finding.file,
                "line_start": af.finding.line_start,
                "line_end": af.finding.line_end,
                "severity": af.finding.severity,
                "summary": af.finding.summary,
                "reviewers": af.reviewers,
                "disposition_detail": downgraded_af.disposition_detail,
            },
            correlation_id=ticket_id,
        )
    # #2009: mirrored onto the embedded sentinel `Review` block too, so the
    # two copies in one artifact are identical by construction — the same
    # treatment `rejected_count` gets at consolidate time.
    review = verdict.review.model_copy(
        update={"downgraded_disposition_count": downgraded}
    )
    return verdict.model_copy(
        update={
            "accepted": accepted,
            "downgraded_disposition_count": downgraded,
            "review": review,
        }
    )
