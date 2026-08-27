"""Diff-anchoring geometry for reviewer findings (#1237).

The "does this citation point at real diff content?" layer: which files and
lines a captured diff touches, how a near-miss line anchor resolves onto a real
one (#1715's ±3 tolerance), how a claimed line range snaps onto added lines
(:func:`_resolve_line_window`) or the wider context-inclusive hunk window
(:func:`_resolve_hunk_window`, #1738), the #1743 enclosing-def fallback for a
structural finding anchored on a ``def`` line, and the #1976 ``-``/``+``
diff-pair rescue for a quote whose true origin is a removed line.

Everything here is geometry and containment — it answers where a citation
lands, never what to do about a citation that lands badly. The classification
that turns these answers into an acceptance or a typed rejection reason lives
in :mod:`cw.review_findings._classify`, which imports this module; the
document-level entry point that composes both lives in
:mod:`cw.review_findings._document`. Import direction is strictly
``_document`` → ``_classify`` → ``_anchor``, never reversed, so the package's
dependency direction stays acyclic.

The pure text-matching primitives these functions delegate to live in
:mod:`cw.review_findings._text_match`, and the content-based rescue in
:mod:`cw.review_findings._reanchor` (both extracted by #2007).

Split out of ``_validation.py`` (#2054), itself split out of the single
``review_findings.py`` module (#1818); import these names from
:mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING

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
    )

_log = logging.getLogger(__name__)


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
    anchor an accepted finding is snapped onto
    (:func:`~cw.review_findings._classify._resolved_finding`, which keeps
    calling this function's unchanged sibling).
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
