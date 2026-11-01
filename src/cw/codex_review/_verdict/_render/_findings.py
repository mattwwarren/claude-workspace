"""The accepted-findings sections and their per-finding annotations.

Every annotation here is display-only: nothing filters, reorders or splits a
finding into a second heading; each one only labels a finding line so the
reader can weight it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._verdict._render._common import _safe, _truncate

if TYPE_CHECKING:
    from cw.review_findings import (
        AcceptedFinding,
        Finding,
        ReviewVerdict,
        Severity,
    )

# Confidence values other than HIGH render an inline annotation on their
# finding line so a reader can weight it — confidence is display-only and
# must never gate/filter/reorder findings (R0, #1555). HIGH is the common
# case and stays unmarked to keep the common path uncluttered.
_CONFIDENCE_ANNOTATION = " _({confidence} confidence)_"

# A non-"fixed" disposition means the finding is no longer blocking — voided
# by an operator ("rejected", #1814), deferred, or never decided ("dropped",
# #1805). Before this annotation existed, `_render_findings` filtered on
# severity alone and discarded `disposition` before the loop body ran, so a
# suppressed MUST_FIX rendered byte-identically to a live one and the posted
# comment lied about its own contents. Display-only, exactly like
# _CONFIDENCE_ANNOTATION above: nothing is filtered, reordered, or split into
# a second heading.
_DISPOSITION_ANNOTATION = " _(suppressed — {disposition}{detail})_"

# #2081: a finding whose line anchor validation dropped because the cited line
# resolved against nothing in the diff (but exists in the file on disk). It
# renders at file level, so without this note it would be indistinguishable
# from a finding the reviewer *filed* at file level — and the adjudicator
# needs to know the location is unverified and the text is what to weigh.
# Display-only, exactly like the two annotations above.
_ANCHOR_DEGRADED_ANNOTATION = (
    " _(line anchor degraded — the cited line did not resolve against the "
    "diff; adjudicate on the finding's text)_"
)

# #2099: the sibling routing, where the line anchor DID resolve and the
# evidence quote did not match its window (a formatter hook rewriting the file
# after the reviewer quoted it is the observed cause). A reader must be able to
# tell the two apart from the comment alone — the location here is a real,
# validated one, and it is the quote that needs re-locating, which is the
# opposite of the annotation above. Display-only, same as every annotation here.
_EVIDENCE_DEGRADED_ANNOTATION = (
    " _(evidence unmatched — the cited line resolved but the quote was not "
    "found in its diff window; re-anchor from the finding's text before "
    "bucketing)_"
)
# #2101: a finding whose file falls outside the approved plan's declared
# scope (`AcceptedFinding.in_plan_scope is False`) — a plan supplied, and the
# file is in neither its `## Files Modified` manifest nor the diff's own
# changed-file set. Display-only, exactly like the annotations above: nothing
# here rejects, drops, or reorders the finding, it only flags it for the
# coordinating session's Checkpoint 3a (4d) plan-scope precedence rule.
_OUT_OF_PLAN_SCOPE_ANNOTATION = " _(outside planned file set)_"

# #2210: the reviewer set `contests_adjudication`, i.e. it is knowingly
# re-raising a finding an operator settled and says what changed. Rendered
# whenever the field is non-blank, matched or not -- the label reports what the
# reviewer CLAIMS, and gating it on an actual ledger match would need the match
# result threaded into this renderer. Display-only, exactly like the
# annotations above: nothing here admits, filters, or reorders the finding.
_CONTEST_ANNOTATION = " _(contests prior adjudication — {claim})_"
# The claim is free text from a model; a comment line is not the place for an
# essay, and GitHub caps a comment body at 65,536 characters.
_CONTEST_CLAIM_MAX = 200


def _disposition_annotation(accepted: AcceptedFinding) -> str:
    """Annotate a finding whose disposition says it is no longer blocking.

    ``""`` for the ``"fixed"`` default (the common case stays uncluttered,
    same convention as HIGH confidence). ``disposition_detail`` is appended
    when the producer recorded one — it carries the *why* (which operator
    comment voided it, which adjudication deferred it), which is the whole
    point of surfacing this on the posted comment rather than only in the
    persisted verdict artifact.
    """
    if accepted.disposition in ("fixed", "unresolved"):
        return ""
    # `disposition_detail` carries a ledger rationale and a finding summary
    # through `_render_suppression_signal`, so it is model/operator text.
    detail = (
        f": {_safe(accepted.disposition_detail)}" if accepted.disposition_detail else ""
    )
    return _DISPOSITION_ANNOTATION.format(
        disposition=accepted.disposition, detail=detail
    )


def _degraded_annotation(finding: Finding) -> str:
    """Annotate a finding validation flagged as adjudication-routed (#2099).

    ``""`` unless ``anchor_degraded`` is set, exactly like the two annotation
    helpers above return ``""`` for the uncluttered common case. The flag has
    carried a reason since #2099, and the two reasons say opposite things about
    the finding's location, so this reads it rather than rendering one message
    for both. A flag with no reason (a verdict artifact persisted before #2099,
    reloaded) keeps the original #2081 wording — the older routing was the only
    one that existed then.
    """
    if not finding.anchor_degraded:
        return ""
    if finding.anchor_degraded_reason == "evidence_not_in_diff":
        return _EVIDENCE_DEGRADED_ANNOTATION
    return _ANCHOR_DEGRADED_ANNOTATION


def _contest_annotation(finding: Finding) -> str:
    """Annotate a finding that knowingly contests a settled decision (#2210).

    ``""`` for the blank default, same uncluttered-common-path convention as
    the annotations above. The claim is whitespace-collapsed and truncated so
    one verbose model cannot dominate the comment.
    """
    claim = _truncate(finding.contests_adjudication, _CONTEST_CLAIM_MAX)
    if not claim:
        return ""
    return _CONTEST_ANNOTATION.format(claim=claim)


def _render_findings(
    verdict: ReviewVerdict, severity: Severity, heading: str
) -> list[str]:
    # Iterates the AcceptedFinding, not just `.finding`, so `disposition` is
    # still in scope in the loop body (#1814/A1).
    accepted = [af for af in verdict.accepted if af.finding.severity == severity]
    if not accepted:
        return []
    lines = [f"### {heading}", ""]
    for af in accepted:
        finding = af.finding
        loc = _safe(finding.file)
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        annotation = (
            ""
            if finding.confidence == "HIGH"
            else _CONFIDENCE_ANNOTATION.format(confidence=finding.confidence)
        )
        suppression = _disposition_annotation(af)
        contest = _contest_annotation(finding)
        degraded = _degraded_annotation(finding)
        out_of_scope = (
            _OUT_OF_PLAN_SCOPE_ANNOTATION if af.in_plan_scope is False else ""
        )
        lines.append(
            f"- **{loc}**{annotation}{suppression}{contest}{degraded}"
            f"{out_of_scope} — {_safe(finding.summary)}"
        )
    lines.append("")
    return lines
