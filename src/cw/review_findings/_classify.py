"""Finding classification and rejection detail (#1237).

The verdict layer over :mod:`cw.review_findings._anchor`'s geometry: given
where a citation lands, decide whether the finding is accepted (``None``) or
which :data:`~cw.review_findings._models.RejectedFindingReason` it earns, in a
fixed check order (severity → evidence-present → ``no_diff_anchor``
short-circuit → file-known → line-anchor → evidence-in-claimed-lines). Also
owns the persisted-anchor repair (:func:`_resolved_finding`) and the
diagnosable ``RejectedFinding.detail`` messages (#1792/#2007) an operator
reads off the sentinel when a rejection needs explaining.

Imports :mod:`cw.review_findings._anchor` and is imported by
:mod:`cw.review_findings._document`, never the reverse — the package's
dependency direction stays acyclic.

Split out of ``_validation.py`` (#2054), itself split out of the single
``review_findings.py`` module (#1818); import these names from
:mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, get_args

from cw.review_findings._anchor import (
    _evidence_in_claimed_lines,
    _file_in_repo_tree,
    _line_reference_valid,
    _resolve_line_window,
)
from cw.review_findings._models import Severity, _is_blank
from cw.review_findings._reanchor import (
    _content_rescue_anchor,
    _line_exceeds_file_length,
    _line_reference_out_of_range_detail,
)
from cw.review_findings._text_match import (
    _LINE_ANCHOR_TOLERANCE,
    _evidence_diff_pair,
    _reconcile_evidence_window,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings._models import (
        CapturedDiff,
        Finding,
        RejectedFindingReason,
    )

_log = logging.getLogger(__name__)

# Valid severity strings, derived from the Literal so the two never drift.
# Used by the defensive severity check in validate_reviewer_document — findings
# may arrive via model_construct (executor adapters, untrusted payloads) that
# bypasses Pydantic's Literal enforcement, so validate re-checks it.
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))


def _resolved_finding(diff: CapturedDiff, finding: Finding) -> Finding:
    """Return *finding* with its line anchors snapped onto the resolved window.

    A file-level finding (both endpoints ``None``) passes through unchanged.
    Any other finding reaching this point has already passed
    ``_line_reference_valid``/``_evidence_in_claimed_lines``, so
    :func:`~cw.review_findings._anchor._resolve_line_window` is expected to
    succeed — persisting the resolved anchor (rather than the reviewer's raw,
    possibly ``_LINE_ANCHOR_TOLERANCE``-lines-off claim) keeps downstream
    consumers of an accepted finding (the verdict comment, the fix-loop
    prompt) pointed at the real changed line. The unlikely resolution-failure
    case (e.g. an unanchored finding whose file isn't in the diff at all)
    returns *finding* unchanged rather than raising (#1715).

    After resolution, :func:`_reconcile_evidence_window` is tried against the
    narrow ``file_line_text`` (added-lines-only) substrate — deliberately NOT
    ``file_window_text`` — so a persisted anchor can be repaired to better
    match its own evidence's true span (#1792) without ever snapping onto a
    context line: this preserves the same #1738 invariant that keeps a
    persisted anchor pointed at real added-line content even when the
    (separately, more permissively matched) evidence-quote check in
    :func:`~cw.review_findings._anchor._evidence_in_claimed_lines` spans
    further via the wider ``file_window_text`` substrate. A reconciliation
    that finds no better match (or whose added-only substrate simply cannot
    reach the evidence's true span) leaves the anchor at its #1715
    near-line-tolerance resolution, unchanged.

    When :func:`~cw.review_findings._anchor._resolve_line_window` fails
    outright — which #2007 made reachable for an *accepted* finding, since a
    wide-drift content rescue can now carry one past classification with a
    citation no tolerance-bounded resolution can repair —
    :func:`_content_rescue_anchor` is tried against that same narrow
    ``file_line_text`` substrate, so the finding persists its true location
    instead of the reviewer's stale one. Deliberately NOT ``file_window_text``,
    even though the classify-path rescue that accepted the finding used it: the
    invariant above is that a persisted anchor points at real added-line
    content, and #2007 does not relax it. A finding rescued on a context line
    at classification therefore keeps its declared anchor here rather than
    being snapped onto that context line.

    Symmetrically, when :func:`_resolve_line_window` DOES succeed but
    :func:`_reconcile_evidence_window` finds no better match at all (as
    opposed to finding one that leaves ``(start, end)`` unchanged), #2019
    tries the same narrower-substrate :func:`_content_rescue_anchor` here
    too — this is the persist-time half of #2019's evidence-gate rescue: a
    finding accepted via :func:`_classify_mislocated_finding`'s wider
    ``file_window_text`` search also gets its *persisted* anchor corrected,
    when that correction is possible without ever pointing at a context line.
    A miss (including one caused only by the narrower substrate lacking a
    context line the wider search relied on) leaves the anchor at its
    #1715/#1743 near-line-tolerance resolution, unchanged — same
    degrade-gracefully shape as the ``resolved is None`` branch above.
    """
    if finding.line_start is None and finding.line_end is None:
        return finding
    candidates = diff.file_line_text.get(finding.file, {})
    resolved = _resolve_line_window(
        diff, finding.file, finding.line_start, finding.line_end
    )
    if resolved is None:
        rescued = _content_rescue_anchor(candidates, finding.evidence)
        if rescued is None:
            return finding
        _log.info(
            "auto-dev: rescued finding's persisted anchor via content-based "
            "re-anchoring (file=%s, line=%d)",
            finding.file,
            rescued[0],
        )
        start, end = rescued
        return _finding_with_anchor(finding, start, end)
    start, end = resolved
    reconciled = _reconcile_evidence_window(candidates, finding.evidence, start, end)
    if reconciled is not None and reconciled != (start, end):
        _log.info(
            "auto-dev: repaired finding's declared line window to match its "
            "own evidence span (file=%s, declared=%d-%d, repaired=%d-%d)",
            finding.file,
            start,
            end,
            reconciled[0],
            reconciled[1],
        )
        start, end = reconciled
    elif reconciled is None:
        rescued = _content_rescue_anchor(candidates, finding.evidence)
        if rescued is not None:
            _log.info(
                "auto-dev: repaired finding's persisted anchor via content-"
                "based re-anchoring beyond the reconciliation bound "
                "(file=%s, declared=%d-%d, repaired=%d-%d)",
                finding.file,
                start,
                end,
                rescued[0],
                rescued[1],
            )
            start, end = rescued
    return _finding_with_anchor(finding, start, end)


def _finding_with_anchor(finding: Finding, start: int, end: int) -> Finding:
    """Copy *finding* with its set line endpoints moved to (*start*, *end*).

    An endpoint the reviewer left ``None`` stays ``None`` — resolution repairs
    a claimed anchor, it never invents one the finding did not make.
    """
    updates: dict[str, int | None] = {}
    if finding.line_start is not None:
        updates["line_start"] = start
    if finding.line_end is not None:
        updates["line_end"] = end
    return finding.model_copy(update=updates)


def _classify_unanchored_file(
    file: str, worktree: Path | None
) -> Literal["unanchored", "unknown_file"]:
    """Classify a finding's file once it's known not to be a diff key (#1632).

    Split out of :func:`_classify_finding` to keep that function's return
    count under the ``PLR0911`` ceiling. ``worktree=None`` (no caller opted
    in) or a failed tree-existence check both return ``"unknown_file"`` —
    today's behavior, byte-identical.
    """
    if worktree is not None and _file_in_repo_tree(worktree, file):
        return "unanchored"
    return "unknown_file"


def _normalization_diagnosis(finding: Finding) -> str:
    """Report which normalization/rescue stages ran before this rejection.

    Appended to :func:`_evidence_window_discrepancy_detail`'s message so a
    future false-reject is diagnosable straight from the sentinel, without
    re-deriving the matcher's stages from the source (#1976). Pure function of
    *finding*'s own declared range and evidence shape — the two booleans that
    decide whether :func:`~cw.review_findings._anchor._diff_pair_rescue` could
    have run at all — so it reports what was *attempted*, never why a given
    attempt failed.
    """
    if finding.line_start is None and finding.line_end is None:
        return (
            "; normalization applied: diff-marker stripping, unicode punctuation "
            "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: not "
            "applicable (file-level fallback has no line anchor for a diff-pair "
            "rescue to resolve against)"
        )
    raw_start = (
        finding.line_start if finding.line_start is not None else finding.line_end
    )
    raw_end = finding.line_end if finding.line_end is not None else finding.line_start
    if raw_start == raw_end and _evidence_diff_pair(finding.evidence) is not None:
        return (
            "; normalization applied: diff-marker stripping, unicode punctuation "
            "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: "
            "attempted (evidence is a -/+ line pair against a 1-line declared range) "
            "but no match in the file's raw diff text either"
        )
    return (
        "; normalization applied: diff-marker stripping, unicode punctuation "
        "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: not "
        "attempted (evidence is not a -/+ line pair against a 1-line declared "
        "range); only the marker-strip and unicode-punctuation normalization "
        "above were applied"
    )


def _evidence_window_discrepancy_detail(finding: Finding) -> str:
    """Build a diagnosable ``RejectedFinding.detail`` for an
    ``evidence_not_in_diff`` rejection (#1792 AC4).

    For a line-anchored finding, only called once it has already passed
    ``_line_reference_valid`` (its endpoints DO resolve to real diff lines) —
    the rejection is about the *span*, not the anchor, so this reports the
    evidence's own line count against the declared window rather than
    re-deriving anchor validity. The no-anchor branch below instead covers a
    file-level finding (``line_start``/``line_end`` both ``None``), which has
    no endpoints for ``_line_reference_valid`` to have validated in the first
    place. Takes only *finding* (no ``diff`` — the message is derived
    entirely from the finding's own declared/evidence shape, not from diff
    content).
    """
    evidence_lines = finding.evidence.count("\n") + 1
    if finding.line_start is None and finding.line_end is None:
        return (
            f"evidence is {evidence_lines} line(s); finding has no line "
            "anchor (file-level fallback) and the evidence text was not "
            "found anywhere in the file's diff" + _normalization_diagnosis(finding)
        )
    declared_lines = (
        (finding.line_end or finding.line_start or 0)
        - (finding.line_start or finding.line_end or 0)
        + 1
    )
    return (
        f"evidence is {evidence_lines} line(s) long but the declared range "
        f"line_start={finding.line_start}, line_end={finding.line_end} spans "
        f"{declared_lines} line(s); no window within ±{_LINE_ANCHOR_TOLERANCE} "
        "lines of the declared range contains the evidence text verbatim; an "
        "unbounded content-based re-anchoring search of the file's diff also "
        "found no match (#2019)"
        + _normalization_diagnosis(finding)
    )


def _rejection_detail(
    finding: Finding, reason: RejectedFindingReason, worktree: Path | None
) -> str:
    """Build the ``RejectedFinding.detail`` for *reason*, or ``""`` if it has none.

    Two of the seven reasons carry a diagnosable message: #1792's
    ``evidence_not_in_diff`` and #2007's ``line_reference_out_of_range``. The
    rest are self-describing from the reason alone. ``worktree`` is guaranteed
    non-``None`` for the out-of-range case — that reason is only reachable via
    a worktree-measured length check — and is re-narrowed here for the type
    checker rather than asserted.
    """
    if reason == "evidence_not_in_diff":
        return _evidence_window_discrepancy_detail(finding)
    if reason == "line_reference_out_of_range" and worktree is not None:
        return _line_reference_out_of_range_detail(finding, worktree)
    return ""


def _classify_drifted_finding(
    finding: Finding, diff: CapturedDiff, worktree: Path | None
) -> RejectedFindingReason | None:
    """Classify a finding whose line anchor missed every tolerance-bounded gate.

    #2007's classify-path rescue, and the two rejection reasons that survive
    it. The evidence text is searched for across the file's whole
    ``file_window_text`` substrate with no line bound — the same substrate
    :func:`~cw.review_findings._anchor._evidence_in_claimed_lines` already
    matches evidence against, so a rescue here accepts nothing that a
    correctly-cited version of the same finding would not have been accepted
    on. A hit returns ``None`` (accepted); :func:`_resolved_finding`
    separately repairs the persisted anchor, against the narrower
    added-lines-only substrate.

    On a miss, the rejection is split by *why* the citation was unusable:
    ``"line_reference_out_of_range"`` when ``worktree`` is given and the cited
    line is past the end of the real file (an invented position), and today's
    ``"invalid_line_reference"`` otherwise. ``worktree=None`` (no caller opted
    in) keeps the pre-#2007 reason byte-for-byte, the same degrade-gracefully
    shape as #1632's ``_classify_unanchored_file``.
    """
    rescued = _content_rescue_anchor(
        diff.file_window_text.get(finding.file, {}), finding.evidence
    )
    if rescued is not None:
        _log.info(
            "auto-dev: rescued finding via content-based re-anchoring "
            "(file=%s, line=%d)",
            finding.file,
            rescued[0],
        )
        return None
    if worktree is not None and any(
        _line_exceeds_file_length(worktree, finding.file, line)
        for line in (finding.line_start, finding.line_end)
        if line is not None
    ):
        return "line_reference_out_of_range"
    return "invalid_line_reference"


def _classify_mislocated_finding(
    finding: Finding, diff: CapturedDiff
) -> RejectedFindingReason | None:
    """Rescue a finding whose valid anchor's window doesn't contain its own
    evidence (#2019).

    Sibling of :func:`_classify_drifted_finding`'s content rescue, scoped to
    the opposite gate: here the line anchor itself resolved fine (it passed
    :func:`~cw.review_findings._anchor._line_reference_valid`), but
    :func:`~cw.review_findings._anchor._evidence_in_claimed_lines` — already
    widened up to :data:`~cw.review_findings._text_match._LINE_ANCHOR_TOLERANCE`
    lines past the resolved window by #1792 — still can't find the evidence.
    Same unbounded ``file_window_text`` search as #2007's sibling, no
    additional line bound: a reviewer's declared line number is the least
    stable part of a citation (rebases and multi-hunk shifts move it), so if
    the evidence text is genuinely present anywhere else in the file's diff,
    that is a stronger signal than the stale line number and the finding is
    accepted rather than mechanically discarded on citation mechanics.
    """
    rescued = _content_rescue_anchor(
        diff.file_window_text.get(finding.file, {}), finding.evidence
    )
    if rescued is not None:
        _log.info(
            "auto-dev: rescued finding via content-based re-anchoring "
            "(evidence-gate) (file=%s, line=%d)",
            finding.file,
            rescued[0],
        )
        return None
    return "evidence_not_in_diff"


def _classify_anchored_finding(
    finding: Finding,
    diff: CapturedDiff,
    changed: frozenset[str],
    worktree: Path | None,
) -> RejectedFindingReason | None:
    """The diff-anchoring half of :func:`_classify_finding`'s check order.

    Split out of that function to keep its return count under the ``PLR0911``
    ceiling once #1817's ``no_diff_anchor`` short-circuit was added — the same
    reason :func:`_classify_unanchored_file` was split out for #1632. Every
    check here presumes the finding claims a diff anchor at all; see
    :func:`_classify_finding`'s docstring for the semantics of each.

    Order: file-known → line-anchor gate → (on a gate miss)
    :func:`_classify_drifted_finding`'s content rescue → evidence-in-claimed-
    lines → (on a gate miss) :func:`_classify_mislocated_finding`'s content
    rescue. Neither gate miss is terminal any more (#2007, #2019): the
    evidence may still be genuinely present in the diff, just further from
    the cited line than ``_LINE_ANCHOR_TOLERANCE`` (and #1792's widening)
    reaches.
    """
    if finding.file not in changed:
        return _classify_unanchored_file(finding.file, worktree)
    if not _line_reference_valid(diff, finding, worktree):
        return _classify_drifted_finding(finding, diff, worktree)
    if not _evidence_in_claimed_lines(
        diff, finding.file, finding.evidence, finding.line_start, finding.line_end
    ):
        return _classify_mislocated_finding(finding, diff)
    return None


def _classify_finding(
    finding: Finding,
    diff: CapturedDiff,
    changed: frozenset[str],
    worktree: Path | None = None,
) -> RejectedFindingReason | None:
    """Return the rejection reason for *finding*, or ``None`` if it passes.

    Check order (first failure wins): severity → evidence-present →
    no-diff-anchor short-circuit → file-known → line-in-range →
    evidence-in-claimed-lines (the last three delegated to
    :func:`_classify_anchored_finding`). The
    ``invalid_line_reference`` check MUST run before the evidence check so that
    ``_evidence_in_claimed_lines`` only builds a window from confirmed-real
    changed lines (possibly snapped within tolerance) — a bogus line reference
    (e.g. ``line_start=999``) is reported
    as ``invalid_line_reference``, not misclassified as ``evidence_not_in_diff``
    via an empty window. The escalation-quote check is NOT here — it runs only
    after a finding passes all of these.

    When *finding*'s file is not in the diff at all, ``worktree`` (when given)
    is consulted as a fallback: a file that genuinely exists in the repo tree
    is "unanchored" rather than "unknown_file" (#1632) — evidence proven only
    by tree-existence, not diff-containment, so the finding is routed to
    adjudication (see
    :func:`~cw.review_findings._document.validate_reviewer_document`) instead
    of being silently discarded. ``worktree=None`` (no caller opted in) or a
    failed tree check both fall back to today's ``"unknown_file"`` behavior,
    and neither ``_line_reference_valid`` nor ``_evidence_in_claimed_lines``
    ever runs for an unanchored finding — there is no diff-line window to
    check it against.

    ``worktree`` also threads into
    :func:`~cw.review_findings._anchor._line_reference_valid` as the
    #1743 enclosing-def fallback: a structural finding anchored on a
    function/class's ``def`` line, which is itself rarely a changed line,
    can now survive this check when a changed line falls inside that
    definition's span. This can turn a previously ``invalid_line_reference``
    case into ``evidence_not_in_diff`` at the very next check below —
    intentional; #1743 owns the anchor-resolution axis, #1738 owns
    evidence-quote matching.

    #1816 investigated this exact ``evidence_not_in_diff`` outcome for a
    whole-function structural claim (evidence describing an aggregate
    property of a function's body rather than quoting any diff line —
    reconstructed in ``tests/test_review_findings.py``'s
    ``Test9491MustFixCaseReconstruction``) and concluded the rejection is
    CORRECT: such a claim has no diff-resident string form at any offset, so
    no window-matching change here could ever satisfy it. No predicate
    change resulted; this is the reviewer/codex output contract's problem
    (see ``.claude/commands/auto-dev-review.md``'s verbatim-evidence
    requirement), not a matcher defect.

    ``no_diff_anchor`` (#1817) short-circuits to acceptance before any
    anchoring check runs: the finding declares it has no diff artifact at all,
    so ``file`` is the ``"N/A"`` literal rather than a diff key and the model
    already guarantees no line anchor — every remaining check would be a
    category error, and the ``"unknown_file"`` verdict they produce today is
    the exact silent-drop that marker exists to close. The severity and
    evidence-present checks above still validly apply to it.
    """
    if finding.severity not in _VALID_SEVERITIES:
        return "invalid_severity"
    if _is_blank(finding.evidence):
        return "missing_evidence"
    if finding.no_diff_anchor:
        return None
    return _classify_anchored_finding(finding, diff, changed, worktree)
