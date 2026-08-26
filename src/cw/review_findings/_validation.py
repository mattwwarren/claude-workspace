"""Mechanical validation of reviewer findings against a captured diff (#1237).

The rejection/acceptance half of the :mod:`cw.review_findings` contract: file
and line-anchor resolution (including #1715's near-line tolerance, #1738's
hunk-context window, #1743's enclosing-def fallback, and #2007's content-based
rescue for a citation that drifted past all of them), evidence-quote matching,
escalation stripping, and the :func:`validate_reviewer_document` entry point
that composes them.

The rescue inventory, in the order a finding meets them: ``_nearest_added_line``
(#1715, ±3 lines) → ``_anchor_in_enclosing_def`` (#1743, worktree opt-in) →
``_content_rescue_anchor`` (#2007, unbounded content search) at the anchor gate;
``_nearest_hunk_line``/``_reconcile_evidence_window`` (#1738/#1792) →
``_diff_pair_rescue`` (#1976) at the evidence-quote gate.

The pure text-matching primitives live in :mod:`cw.review_findings._text_match`
and the content-based rescue in :mod:`cw.review_findings._reanchor`, both
extracted by #2007 — the latter is imported here, never the reverse, so the
package's dependency direction stays acyclic.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING, Literal, get_args

from cw.review_findings._models import (
    _ESCALATION_STRIP_REASON,
    RejectedFinding,
    Severity,
    StrippedEscalation,
    _is_blank,
)
from cw.review_findings._reanchor import (
    _content_rescue_anchor,
    _line_exceeds_file_length,
    _line_reference_out_of_range_detail,
)
from cw.review_findings._text_match import (
    _LINE_ANCHOR_TOLERANCE,
    _evidence_diff_pair,
    _normalize_diff_text,
    _reconcile_evidence_window,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings._models import (
        CapturedDiff,
        Finding,
        RejectedFindingReason,
        ReviewerFindingsDocument,
    )

_log = logging.getLogger(__name__)

# Valid severity strings, derived from the Literal so the two never drift.
# Used by the defensive severity check in validate_reviewer_document — findings
# may arrive via model_construct (executor adapters, untrusted payloads) that
# bypasses Pydantic's Literal enforcement, so validate re-checks it.
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))


def _changed_files(diff: CapturedDiff) -> frozenset[str]:
    """Return the set of file paths touched by *diff*."""
    return frozenset(diff.files)


def _line_in_diff(diff: CapturedDiff, file: str, line: int) -> bool:
    """Return True iff *line* is a changed line of *file* in *diff*."""
    return line in diff.files.get(file, [])


def _substring_in_diff(diff: CapturedDiff, text: str) -> bool:
    """Return True iff *text* appears anywhere in the diff text.

    Matches against the FULL diff text (context and removed lines included),
    not only ``+``-prefixed added lines — same rule for finding evidence and
    escalation evidence quotes. Both sides are normalized through
    :func:`_normalize_diff_text`, identically to the primary finding-evidence
    path, so an escalation quote is not stripped over a stray diff marker or a
    Unicode dash standing in for its ASCII form (#1976). The ``-``/``+``
    diff-pair rescue is deliberately NOT wired here: an
    ``EscalationMetadata`` carries no line anchor for it to resolve a 1-line
    declared range against.
    """
    return _normalize_diff_text(text) in _normalize_diff_text(diff.text)


def _file_in_repo_tree(worktree: Path, file: str) -> bool:
    """True iff *file* resolves to a real file under *worktree*.

    Guards against a hallucinated/adversarial ``Finding.file`` escaping the
    worktree via an absolute path (pathlib's ``/`` operator silently
    discards the left operand when the right is absolute) or a ``../``
    traversal — the joined-and-resolved candidate must stay under the
    resolved worktree root. Proves the *path* is real; callers must not
    treat this as evidence the *quote* is real (R2, #1632).
    """
    try:
        root = worktree.resolve()
        candidate = (worktree / file).resolve()
    except OSError:
        return False
    return candidate.is_relative_to(root) and candidate.is_file()


def _nearest_added_line(
    diff: CapturedDiff, file: str, line: int, tolerance: int = _LINE_ANCHOR_TOLERANCE
) -> int | None:
    """Return the added line of *file* nearest *line*, within *tolerance*.

    An exact hit (*line* itself is a changed line) short-circuits via
    :func:`_line_in_diff` without scanning candidates. Otherwise the nearest
    candidate in ``diff.files.get(file, [])`` within *tolerance* lines wins;
    ties break by lowest distance, then lowest line number. Returns ``None``
    when no added line is within tolerance, including when *file* has none at
    all (#1715).
    """
    if _line_in_diff(diff, file, line):
        return line
    best: int | None = None
    best_distance = tolerance + 1
    for candidate in diff.files.get(file, []):
        distance = abs(candidate - line)
        if distance > tolerance:
            continue
        if (
            best is None
            or distance < best_distance
            or (distance == best_distance and candidate < best)
        ):
            best, best_distance = candidate, distance
    return best


def _nearest_hunk_line(
    diff: CapturedDiff, file: str, line: int, tolerance: int = _LINE_ANCHOR_TOLERANCE
) -> int | None:
    """Return the hunk-covered line of *file* nearest *line*, within *tolerance*.

    Sibling of :func:`_nearest_added_line`, deliberately kept separate rather
    than repurposing it (#1738): candidates are drawn from
    ``diff.file_window_text.get(file, {})`` — every context OR added line the
    diff covers, not only added lines — so this must never be substituted for
    :func:`_nearest_added_line` at :func:`_line_reference_valid`'s
    anchor-validity call site. Same exact-hit short-circuit and tie-break
    rules as :func:`_nearest_added_line`.
    """
    if line in diff.file_window_text.get(file, {}):
        return line
    best: int | None = None
    best_distance = tolerance + 1
    for candidate in diff.file_window_text.get(file, {}):
        distance = abs(candidate - line)
        if distance > tolerance:
            continue
        if (
            best is None
            or distance < best_distance
            or (distance == best_distance and candidate < best)
        ):
            best, best_distance = candidate, distance
    return best


def _enclosing_def_span(source: str, line: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` line span of the innermost function or
    class in *source* enclosing *line*, or ``None`` if none does.

    Walks every :class:`ast.FunctionDef`/:class:`ast.AsyncFunctionDef`/
    :class:`ast.ClassDef` node and picks the smallest span containing *line*,
    so a nested function's span wins over its enclosing function's. A
    decorated function's span starts at the ``def``/``class`` line itself
    (Python's ``lineno`` for such nodes, since 3.8, excludes the decorator
    lines) — a line that only touches a decorator has no enclosing span.
    *source* that fails to parse (a syntax error, or ``ast.parse``'s
    ``ValueError`` on embedded null bytes) returns ``None`` rather than
    raising — the caller (:func:`_anchor_in_enclosing_def`) treats "can't
    determine a span" the same as "no span" (#1743).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end = node.end_lineno
        if end is None or not (node.lineno <= line <= end):
            continue
        if best is None or (end - node.lineno) < (best[1] - best[0]):
            best = (node.lineno, end)
    return best


def _anchor_in_enclosing_def(
    diff: CapturedDiff, worktree: Path, file: str, line: int
) -> bool:
    """True iff *line*'s enclosing def/class span in *file* contains a
    changed line of *diff*.

    Reads ``(worktree / file).read_text()`` and resolves *line*'s span via
    :func:`_enclosing_def_span`; a missing/unreadable file or a line with no
    enclosing definition both return ``False``. Gives a structural finding
    (too-long function, too-many-params, does-two-things) anchored on the
    enclosing ``def``/``class`` line — which is itself rarely a changed line —
    a legitimate way to anchor: the span, not the single def line, is checked
    against the diff's changed lines (#1743).
    """
    try:
        source = (worktree / file).read_text()
    except (OSError, UnicodeDecodeError):
        return False
    span = _enclosing_def_span(source, line)
    if span is None:
        return False
    start, end = span
    return any(start <= changed <= end for changed in diff.files.get(file, []))


def _line_reference_valid(
    diff: CapturedDiff, finding: Finding, worktree: Path | None = None
) -> bool:
    """Return True iff *finding*'s line references resolve to a changed line.

    A file-level finding (both endpoints ``None``) is exempt — it has no line
    anchor to check. A near-miss anchor within ``_LINE_ANCHOR_TOLERANCE`` lines
    of a real changed line resolves via :func:`_nearest_added_line` rather than
    requiring an exact match (#1715).

    ``worktree`` (default ``None``) opts into the #1743 enclosing-def
    fallback: when an endpoint is not itself near a changed line, and
    ``worktree`` is given, :func:`_anchor_in_enclosing_def` is tried before
    giving up on that endpoint — this is what lets a structural finding
    anchored on a function/class's ``def`` line survive even though that line
    is rarely itself changed, as long as some changed line falls inside the
    definition's span. ``worktree=None`` (no caller opted in) disables the
    fallback entirely, matching today's behavior byte-for-byte — the same
    opt-in shape as #1632's ``_classify_unanchored_file``.
    """
    for line in (finding.line_start, finding.line_end):
        if line is None:
            continue
        if _nearest_added_line(diff, finding.file, line) is not None:
            continue
        if worktree is not None and _anchor_in_enclosing_def(
            diff, worktree, finding.file, line
        ):
            _log.info(
                "auto-dev: rescued finding anchored on enclosing def/class "
                "span not itself a changed line (file=%s, line=%d)",
                finding.file,
                line,
            )
            continue
        return False
    return True


def _resolve_line_window(
    diff: CapturedDiff, file: str, line_start: int | None, line_end: int | None
) -> tuple[int, int] | None:
    """Resolve a claimed (``line_start``, ``line_end``) pair to real added lines.

    Callers must not pass both endpoints ``None`` (that's the file-level case,
    handled separately). A set endpoint resolves via :func:`_nearest_added_line`;
    an unset endpoint mirrors the other before resolving, matching the
    single-line-claim behavior ``_evidence_in_claimed_lines`` has always had.
    Returns the resolved ``(start, end)`` in ascending order, or ``None`` if
    either endpoint fails to resolve within tolerance (#1715).
    """
    raw_start = line_start if line_start is not None else line_end
    raw_end = line_end if line_end is not None else line_start
    if raw_start is None or raw_end is None:
        msg = "_resolve_line_window requires at least one endpoint set"
        raise ValueError(msg)
    resolved_start = _nearest_added_line(diff, file, raw_start)
    resolved_end = _nearest_added_line(diff, file, raw_end)
    if resolved_start is None or resolved_end is None:
        return None
    return min(resolved_start, resolved_end), max(resolved_start, resolved_end)


def _resolve_hunk_window(
    diff: CapturedDiff, file: str, line_start: int | None, line_end: int | None
) -> tuple[int, int] | None:
    """Resolve a claimed (``line_start``, ``line_end``) pair to hunk-covered lines.

    Mirror of :func:`_resolve_line_window`, calling :func:`_nearest_hunk_line`
    (candidates: every context OR added line, via ``file_window_text``)
    instead of :func:`_nearest_added_line` on both endpoints (#1738). Wired
    into exactly one call site — :func:`_evidence_in_claimed_lines`'s windowed
    branch — so the widened recall only affects evidence *matching*, never the
    anchor-*validity* gate (:func:`_line_reference_valid`) or the persisted
    anchor an accepted finding is snapped onto (:func:`_resolved_finding`,
    which keeps calling this function's unchanged sibling).
    """
    raw_start = line_start if line_start is not None else line_end
    raw_end = line_end if line_end is not None else line_start
    if raw_start is None or raw_end is None:
        msg = "_resolve_hunk_window requires at least one endpoint set"
        raise ValueError(msg)
    resolved_start = _nearest_hunk_line(diff, file, raw_start)
    resolved_end = _nearest_hunk_line(diff, file, raw_end)
    if resolved_start is None or resolved_end is None:
        return None
    return min(resolved_start, resolved_end), max(resolved_start, resolved_end)


def _diff_pair_rescue(
    diff: CapturedDiff,
    file: str,
    text: str,
    line_start: int | None,
    line_end: int | None,
) -> bool:
    """Rescue a ``-``/``+`` pair quoted against a 1-line declared range (#1976).

    The windowed check in :func:`_evidence_in_claimed_lines` structurally
    cannot match such a quote: a removed line has no new-file line number, so
    its content is absent from both ``file_line_text`` and
    ``file_window_text`` — ``file_diffs`` (raw per-file hunk text) is the only
    substrate that retains it. Scoped deliberately narrowly: the declared
    range must resolve to a single line (unset endpoint mirroring the other,
    the same rule :func:`_resolve_line_window`/:func:`_resolve_hunk_window`
    use) and the evidence must be pair-shaped, so a multi-line claim gets no
    rescue.

    #1714's false-accept floor is preserved: either half must appear verbatim
    (after :func:`_normalize_diff_text`) in the file's own raw diff text, so
    fabricated content stays rejected. No additional line-number bound is
    applied to the match — that floor is about fabricated content, not
    pinpoint locality.
    """
    raw_start = line_start if line_start is not None else line_end
    raw_end = line_end if line_end is not None else line_start
    if raw_start is None or raw_end is None or raw_start != raw_end:
        return False
    pair = _evidence_diff_pair(text)
    if pair is None:
        return False
    removed, added = pair
    haystack = _normalize_diff_text(diff.file_diffs.get(file, ""))
    if (
        _normalize_diff_text(removed) in haystack
        or _normalize_diff_text(added) in haystack
    ):
        _log.info(
            "auto-dev: rescued finding via diff-pair evidence match against "
            "raw hunk text (file=%s, line=%d)",
            file,
            raw_start,
        )
        return True
    return False


def _evidence_in_claimed_lines(
    diff: CapturedDiff,
    file: str,
    text: str,
    line_start: int | None,
    line_end: int | None,
) -> bool:
    """True iff *text* appears within the finding's claimed line window for *file*.

    A file-level finding (both endpoints ``None``) has no line anchor — falls
    back to file-level matching against that file's full hunk text
    (``file_diffs``), the same fallback ``_line_reference_valid`` already grants
    file-level findings today.

    When a line window is claimed, :func:`_resolve_hunk_window` snaps both
    endpoints (the same near-line tolerance ``_line_reference_valid`` applies,
    but over the wider ``file_window_text`` candidate set — context lines
    included) and orders them ascending; :func:`_reconcile_evidence_window`
    then checks *text* against that window's content and, if the window
    itself is a few lines short/long of *text*'s own true span, against a
    widened window within ``_LINE_ANCHOR_TOLERANCE`` lines (#1792) — an
    endpoint that fails to resolve, or a widened search that finds no
    matching window, falls through to :func:`_diff_pair_rescue` rather than
    failing outright (#1976). Both sides of every substring comparison are
    routed through :func:`_normalize_diff_text`, so neither a stray ``+``/``-``
    diff marker (#1715) nor Unicode punctuation standing in for its ASCII
    equivalent (#1976) on either the evidence or the diff-derived text can
    break an otherwise-genuine match.

    A quote whose true origin is a REMOVED line — which has no new-file line
    number at all — is correctly rejected here (#1738: a context line, unlike
    a removed line, does have a real new-file position and IS captured in
    ``file_window_text``, so it is no longer excluded the way this docstring
    used to claim).
    """
    if line_start is None and line_end is None:
        return _normalize_diff_text(text) in _normalize_diff_text(
            diff.file_diffs.get(file, "")
        )
    resolved = _resolve_hunk_window(diff, file, line_start, line_end)
    if resolved is None:
        return _diff_pair_rescue(diff, file, text, line_start, line_end)
    start, end = resolved
    window_text = diff.file_window_text.get(file, {})
    if _reconcile_evidence_window(window_text, text, start, end) is not None:
        return True
    return _diff_pair_rescue(diff, file, text, line_start, line_end)


def _resolved_finding(diff: CapturedDiff, finding: Finding) -> Finding:
    """Return *finding* with its line anchors snapped onto the resolved window.

    A file-level finding (both endpoints ``None``) passes through unchanged.
    Any other finding reaching this point has already passed
    ``_line_reference_valid``/``_evidence_in_claimed_lines``, so
    :func:`_resolve_line_window` is expected to succeed — persisting the
    resolved anchor (rather than the reviewer's raw, possibly
    ``_LINE_ANCHOR_TOLERANCE``-lines-off claim) keeps downstream consumers of
    an accepted finding (the verdict comment, the fix-loop prompt) pointed at
    the real changed line. The unlikely resolution-failure case (e.g. an
    unanchored finding whose file isn't in the diff at all) returns *finding*
    unchanged rather than raising (#1715).

    After resolution, :func:`_reconcile_evidence_window` is tried against the
    narrow ``file_line_text`` (added-lines-only) substrate — deliberately NOT
    ``file_window_text`` — so a persisted anchor can be repaired to better
    match its own evidence's true span (#1792) without ever snapping onto a
    context line: this preserves the same #1738 invariant that keeps a
    persisted anchor pointed at real added-line content even when the
    (separately, more permissively matched) evidence-quote check in
    :func:`_evidence_in_claimed_lines` spans further via the wider
    ``file_window_text`` substrate. A reconciliation that finds no better
    match (or whose added-only substrate simply cannot reach the evidence's
    true span) leaves the anchor at its #1715 near-line-tolerance resolution,
    unchanged.

    When :func:`_resolve_line_window` fails outright — which #2007 made
    reachable for an *accepted* finding, since a wide-drift content rescue can
    now carry one past classification with a citation no tolerance-bounded
    resolution can repair — :func:`_content_rescue_anchor` is tried against
    that same narrow ``file_line_text`` substrate, so the finding persists its
    true location instead of the reviewer's stale one. Deliberately NOT
    ``file_window_text``, even though the classify-path rescue that accepted
    the finding used it: the invariant above is that a persisted anchor points
    at real added-line content, and #2007 does not relax it. A finding rescued
    on a context line at classification therefore keeps its declared anchor
    here rather than being snapped onto that context line.
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
    decide whether :func:`_diff_pair_rescue` could have run at all — so it
    reports what was *attempted*, never why a given attempt failed.
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
        "lines of the declared range contains the evidence text verbatim"
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
    :func:`_evidence_in_claimed_lines` already matches evidence against, so a
    rescue here accepts nothing that a correctly-cited version of the same
    finding would not have been accepted on. A hit returns ``None``
    (accepted); :func:`_resolved_finding` separately repairs the persisted
    anchor, against the narrower added-lines-only substrate.

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
    lines. A line-anchor miss is no longer terminal (#2007): the evidence may
    still be genuinely present in the diff, just further from the cited line
    than ``_LINE_ANCHOR_TOLERANCE`` reaches.
    """
    if finding.file not in changed:
        return _classify_unanchored_file(finding.file, worktree)
    if not _line_reference_valid(diff, finding, worktree):
        return _classify_drifted_finding(finding, diff, worktree)
    if not _evidence_in_claimed_lines(
        diff, finding.file, finding.evidence, finding.line_start, finding.line_end
    ):
        return "evidence_not_in_diff"
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
    adjudication (see :func:`validate_reviewer_document`) instead of being
    silently discarded. ``worktree=None`` (no caller opted in) or a failed
    tree check both fall back to today's ``"unknown_file"`` behavior, and
    neither ``_line_reference_valid`` nor ``_evidence_in_claimed_lines`` ever
    runs for an unanchored finding — there is no diff-line window to check it
    against.

    ``worktree`` also threads into :func:`_line_reference_valid` as the
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


def validate_reviewer_document(
    doc: ReviewerFindingsDocument, diff: CapturedDiff, *, worktree: Path | None = None
) -> tuple[list[Finding], list[RejectedFinding], list[StrippedEscalation]]:
    """Validate one reviewer's findings against *diff*.

    Returns ``(accepted, rejected, stripped)``. A finding that passes all core
    checks but carries an escalation whose ``evidence_quote`` is not in the
    diff is accepted with its escalation nulled, and a
    :class:`StrippedEscalation` is recorded (with a WARNING log). A finding
    that is itself rejected never reaches the escalation-quote check.

    ``worktree`` (default ``None``) opts into the #1632 unanchored-finding
    relaxation: when a finding's ``file`` is not part of *diff* but resolves
    to a real file under ``worktree``, :func:`_classify_finding` returns
    ``"unanchored"`` instead of ``"unknown_file"`` — this function then routes
    it into ``accepted`` (with an INFO log) rather than constructing a
    :class:`RejectedFinding`, so it reaches adjudication instead of being
    silently discarded. Tree-existence proves only that the *path* is real,
    not that the finding's evidence quote is — an unanchored finding's
    escalation (if any) still goes through the ordinary diff-based
    evidence_quote check below, same as any other accepted finding.
    """
    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []
    stripped: list[StrippedEscalation] = []
    changed = _changed_files(diff)

    for index, finding in enumerate(doc.findings):
        reason = _classify_finding(finding, diff, changed, worktree)
        if reason is not None and reason != "unanchored":
            # #2000: announce EVERY mechanical rejection, at every severity.
            # Before this line, a rejection below MUST_FIX left no trace on any
            # surface -- #1714 gave the MUST_FIX case a verdict field and a
            # force-block, but a SHOULD_FIX/DEBT/NIT/PRINCIPLE finding was
            # deleted here in silence and the run reported as if nothing had
            # been found. INFO (not WARNING) mirrors the "unanchored" /
            # "no_diff_anchor" routing logs below: same category of event,
            # same level.
            _log.info(
                "auto-dev: mechanically rejected finding — will not reach "
                "adjudication (reviewer_role=%s, finding_index=%d, "
                "severity=%s, reason=%s, title=%s)",
                doc.reviewer_role,
                index,
                finding.severity,
                reason,
                finding.summary,
            )
            rejected.append(
                RejectedFinding(
                    raw=finding.model_dump(),
                    reviewer_role=doc.reviewer_role,
                    reason=reason,
                    detail=_rejection_detail(finding, reason, worktree),
                )
            )
            continue
        if reason == "unanchored":
            _log.info(
                "auto-dev: routed unanchored finding to adjudication (file "
                "exists in repo tree, not diff-anchored; evidence quote not "
                "verified) (reviewer_role=%s, finding_index=%d, file=%s)",
                doc.reviewer_role,
                index,
                finding.file,
            )
        if finding.no_diff_anchor:
            # Mirrors the "unanchored" INFO above: a finding that skipped the
            # mechanical checks is always announced, so an operator can tell a
            # genuinely-verified acceptance from a marker-driven one.
            _log.info(
                "auto-dev: routed no_diff_anchor finding to adjudication "
                "(remedy is outside the diff; no mechanical anchor check "
                "performed) (reviewer_role=%s, finding_index=%d, severity=%s)",
                doc.reviewer_role,
                index,
                finding.severity,
            )
        resolved_finding = _resolved_finding(diff, finding)
        escalation = finding.escalation
        if escalation is not None and not _substring_in_diff(
            diff, escalation.evidence_quote
        ):
            # Log the dropped escalation before appending, mirroring
            # auto_dev_result._filter_empty_pending_items' convention of always
            # logging silently-dropped payload content.
            _log.warning(
                "auto-dev: stripped escalation with evidence_quote not found in "
                "diff (reviewer_role=%s, finding_index=%d, target_reviewer=%s)",
                doc.reviewer_role,
                index,
                escalation.target_reviewer,
            )
            stripped.append(
                StrippedEscalation(
                    reviewer_role=doc.reviewer_role,
                    finding_index=index,
                    target_reviewer=escalation.target_reviewer,
                    reason=_ESCALATION_STRIP_REASON,
                )
            )
            accepted.append(resolved_finding.model_copy(update={"escalation": None}))
        else:
            accepted.append(resolved_finding)

    return accepted, rejected, stripped
