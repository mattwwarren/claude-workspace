"""Pure text-normalization and window-reconciliation primitives (#1715/#1976/#1792).

Extracted verbatim by #2007 from the then-single ``_validation`` module, since
split three ways into :mod:`cw.review_findings._anchor`,
:mod:`cw.review_findings._classify`, and :mod:`cw.review_findings._document`
(#2054). These functions were always leaf-level — stdlib only, no model or diff
dependency — but they lived in the module that also owns classification. #2007
added a second consumer (:mod:`cw.review_findings._reanchor`, the content-based
rescue) which the validation modules in turn call, so leaving them where they
were would have forced a validation <-> ``_reanchor`` import cycle. Hoisting
them into a shared leaf both import from breaks it by construction.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

# Reviewer-supplied line anchors observed drifting off the true added line by
# one to three lines in fleet review runs (#1715) — usually a stale line
# number from a prior diff revision or an off-by-one miscount, with otherwise
# correct evidence text. Fixed module constant, not derived from hunk/file
# size (see _nearest_added_line).
_LINE_ANCHOR_TOLERANCE: int = 3  # lines

# A `-`/`+` evidence pair is exactly two lines: the removed one and the added
# one. Anything longer is a multi-line quote, not the single-rewritten-line
# shape the rescue is scoped to (#1976).
_EVIDENCE_PAIR_LINES: int = 2

# Unicode punctuation an LLM reviewer routinely substitutes for its ASCII
# equivalent when quoting a diff (#1976) — an em dash for ``--`` is the shape
# that motivated the ticket. Keys are code points rather than literal
# characters so this module stays pure-ASCII: ruff's RUF001 flags several of
# these as ambiguous inside a string literal.
_UNICODE_PUNCTUATION: dict[int, str] = {
    0x2014: "--",  # EM DASH
    0x2013: "-",  # EN DASH
    0x2018: "'",  # LEFT SINGLE QUOTATION MARK
    0x2019: "'",  # RIGHT SINGLE QUOTATION MARK
    0x201C: '"',  # LEFT DOUBLE QUOTATION MARK
    0x201D: '"',  # RIGHT DOUBLE QUOTATION MARK
    0x00A0: " ",  # NO-BREAK SPACE
}


def _strip_diff_markers(text: str) -> str:
    """Strip one leading diff marker and surrounding whitespace per line.

    Lets an evidence-vs-diff substring comparison match regardless of which
    side (or neither) carries a stray ``+``/``-`` prefix: a reviewer's evidence
    quote copied straight from a rendered diff view carries the marker even
    though it isn't part of the real source line, while the file-level
    fallback (``file_diffs``) always carries one because it stores raw
    per-file hunk text. Only a single leading marker character is stripped per
    line — line count and order are preserved (no blank-line collapsing), so
    this cannot merge or reorder content (#1715).
    """
    normalized_lines = []
    for raw_line in text.split("\n"):
        stripped_marker = raw_line[1:] if raw_line[:1] in ("+", "-") else raw_line
        normalized_lines.append(stripped_marker.strip())
    return "\n".join(normalized_lines)


def _normalize_unicode_punctuation(text: str) -> str:
    """Fold the Unicode punctuation in *text* onto its ASCII equivalent.

    Maps EM DASH to ``--``, EN DASH to ``-``, both curly single quotes to
    ``'``, both curly double quotes to ``"``, and NO-BREAK SPACE to a plain
    space. A reviewer quoting a diff through an LLM routinely emits the
    typographic form where the source carries the ASCII one, which a raw
    substring comparison reads as a content mismatch rather than the
    formatting trivia it is (#1976). Nothing else is folded: this is a fixed,
    enumerated table, not a general Unicode normalization pass.
    """
    return text.translate(_UNICODE_PUNCTUATION)


def _normalize_diff_text(text: str) -> str:
    """Normalize *text* for evidence-vs-diff substring comparison.

    The single chokepoint every such comparison in this package routes both
    sides through: :func:`_strip_diff_markers` first (#1715), then
    :func:`_normalize_unicode_punctuation` (#1976). Strictly wider than the
    marker strip alone — it never rejects a pair the marker strip would have
    matched.
    """
    return _normalize_unicode_punctuation(_strip_diff_markers(text))


def _evidence_diff_pair(text: str) -> tuple[str, str] | None:
    """Return *text*'s ``(removed, added)`` halves iff it is a ``-``/``+`` pair.

    A reviewer quoting a single rewritten line commonly quotes the diff's own
    two-line before/after pair rather than the resulting source line (#1976).
    Exactly two lines, the first ``-``-prefixed and the second ``+``-prefixed,
    qualify; anything else (one line, three or more, both removed, reversed
    order) returns ``None``.
    """
    lines = text.split("\n")
    if len(lines) != _EVIDENCE_PAIR_LINES:
        return None
    removed, added = lines
    if not removed.startswith("-") or not added.startswith("+"):
        return None
    return removed, added


def _reconcile_evidence_window(
    candidates: dict[int, str],
    text: str,
    start: int,
    end: int,
    tolerance: int = _LINE_ANCHOR_TOLERANCE,
) -> tuple[int, int] | None:
    """Reconcile a declared (start, end) window against *text*'s own content
    when the window itself is too short/long by a few lines (#1792,
    producer-side variant of #1715/#1738/#1743).

    Two-phase, deliberately asymmetric in strictness:

    1. The declared ``(start, end)`` window unchanged (widen=0,0) is tried
       first, using the exact pre-#1792 rule: a gap-tolerant join of
       whichever of ``range(start, end + 1)`` are present in *candidates*
       (a missing line is silently skipped, never synthesized), then a
       plain substring check against *text*. This reproduces pre-#1792
       behavior byte-for-byte — required so every already-passing
       #1236/#1715/#1738 case is untouched, including ones where the
       evidence is a short fragment inside a wider, near-line-tolerance-
       widened window that itself has gaps.
    2. Only when that fails does this grow the start backward and/or the
       end forward by up to *tolerance* lines each. A widened candidate
       counts ONLY when every line in its range is present in *candidates*
       (no gaps — never synthesizes a line the diff doesn't contain) AND
       its joined content EXACTLY equals *text* (both normalized) — not
       merely contains it. Exact equality (stricter than phase 1's
       substring check) is load-bearing: a substring check here would let
       widening accidentally absorb a different, genuinely-real-but-
       unrelated adjacent line into the window purely because it happens
       to contain the evidence text as a substring once joined (regression
       guard: a 1-line quote actually anchored on the line right after a
       claimed single-line window, with the declared line prepended as
       unrelated "noise", must stay rejected — #1236 R6's claimed-window
       boundary). Phase 2 is what actually *widens* the window; phase 1
       never does, so it can never be fooled by an adjacent real line the
       way an unconstrained substring-under-widening search would be.

    ``candidates`` is a ``{line: content}`` map — callers pass either the
    narrow added-lines-only substrate for anchor persistence or the wider
    hunk-context substrate for evidence matching, never both. Returns the
    smallest matching widened window (by span, then lowest start) so a
    repaired anchor is the tightest defensible span. ``None`` when neither
    phase finds a match — a genuinely-absent evidence string is unaffected
    by this function at any offset (#1714's false-accept guard is
    preserved: this only ever repairs a length mismatch on real, fully and
    exactly present content, never accepts fabricated or unrelated
    content).
    """
    target = _normalize_diff_text(text)
    base_joined = "\n".join(
        candidates[n] for n in range(start, end + 1) if n in candidates
    )
    if target in _normalize_diff_text(base_joined):
        return (start, end)

    best: tuple[int, int] | None = None
    for widen_start in range(tolerance + 1):
        for widen_end in range(tolerance + 1):
            if widen_start == 0 and widen_end == 0:
                continue  # already tried above (phase 1)
            candidate_start = start - widen_start
            candidate_end = end + widen_end
            if candidate_start > candidate_end:
                continue
            line_range = range(candidate_start, candidate_end + 1)
            if not all(n in candidates for n in line_range):
                continue
            joined = "\n".join(candidates[n] for n in line_range)
            if _normalize_diff_text(joined) != target:
                continue
            span = candidate_end - candidate_start
            if (
                best is None
                or span < (best[1] - best[0])
                or (span == (best[1] - best[0]) and candidate_start < best[0])
            ):
                best = (candidate_start, candidate_end)
    return best
