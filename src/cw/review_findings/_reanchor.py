"""Content-based re-anchoring for findings whose line citation drifted (#2007).

#1715's ``_LINE_ANCHOR_TOLERANCE`` repairs a citation that missed its true line
by one to three. Beyond that it gives up, and a 3,318-transcript scan found the
cost: 52 ``fixed`` dispositions silently downgraded to ``dropped`` across ~31
shipped tickets, plus live ``invalid_line_reference`` false-rejects — all on
findings whose evidence text was genuinely present in the diff, just further
away than the tolerance reaches (an earlier hunk grew, shifting everything
below it).

The rescue here is content-based rather than proximity-based: it searches the
whole substrate for the evidence text with no line bound at all, and is
strictly additive — a rescue that finds nothing leaves the existing rejection
or ``"dropped"`` disposition exactly as it was. #1714's false-accept floor is
inherited unchanged, because the actual matching is delegated to
:func:`_reconcile_evidence_window` rather than reimplemented.

Three call sites, each with its own deliberately-chosen substrate:

- classification (:func:`~cw.review_findings._classify._classify_anchored_finding`)
  searches ``file_window_text`` — context and added lines, matching what the
  evidence-quote check already accepts;
- anchor persistence (:func:`~cw.review_findings._classify._resolved_finding`)
  searches ``file_line_text`` — added lines only, preserving #1738's invariant
  that a persisted anchor never points at a context line;
- fix substantiation (:func:`~cw.review_adjudication._fix_is_substantiated`)
  searches ``file_diffs`` for a genuinely REMOVED line, which has no new-file
  line number and so exists in no other substrate.

Split out of the single ``review_findings.py`` module (#1818); import these
names from :mod:`cw.review_findings`, not from this private submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_findings._text_match import (
    _formatting_normalized_text,
    _normalize_diff_text,
    _reconcile_evidence_window,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings._models import Finding


def _content_rescue_anchor(
    candidates: dict[int, str], evidence: str
) -> tuple[int, int] | None:
    """Search *candidates* for *evidence*, unbounded by any declared line.

    The wide-drift sibling of ``_evidence_in_claimed_lines``'s
    tolerance-bounded window match (#2007). A pure function of
    ``(candidates, evidence)``, matching :func:`_reconcile_evidence_window`'s
    own shape: the caller supplies the substrate, which is what scopes the
    search to one file and decides whether context lines are eligible (see
    the module docstring's three call sites).

    Each candidate line in ascending order is tried as the start of a window
    as long as *evidence*'s own line count, delegating the actual match to
    :func:`_reconcile_evidence_window` rather than reimplementing it — so
    #1714's false-accept floor (no gap synthesis, exact-equality widening)
    and #1976's normalization both carry over unchanged. The lowest matching
    window wins.

    Sizing each candidate window by *evidence*'s own line count is what makes
    that first pass cheap, and also what makes it blind to a quote the diff
    wraps across a DIFFERENT number of lines than the quote itself uses - the
    formatter-reflow shape (#2099). So a miss falls through to
    :func:`_formatting_tolerant_window`, which sizes its windows by content
    instead. Strictly additive in the same sense as everything above it: it
    runs only after the line-count-sized search has already found nothing.
    """
    if not candidates:
        return None
    evidence_lines = evidence.count("\n") + 1
    for start in sorted(candidates):
        end = start + evidence_lines - 1
        found = _reconcile_evidence_window(candidates, evidence, start, end)
        if found is not None:
            return found
    return _formatting_tolerant_window(candidates, evidence)


def _formatting_tolerant_window(
    candidates: dict[int, str], evidence: str
) -> tuple[int, int] | None:
    """Search *candidates* for *evidence* ignoring how either side is wrapped.

    :func:`_content_rescue_anchor`'s reflow-tolerant fallback (#2099). Every
    other search in this package compares line for line, so it can only match a
    quote spanning the same lines the diff does; a formatter hook that re-wraps
    a file between the diff capture and the reviewer's read breaks exactly that
    assumption, in both directions (a call joined onto one line in the quote
    and wrapped over four in the diff, or the reverse).

    Each candidate line in ascending order is tried as a window start, and the
    window grows one CONTIGUOUS line at a time (a gap ends the run - never
    synthesize a line the diff does not contain, the same floor
    :func:`_reconcile_evidence_window` holds) until
    :func:`~cw.review_findings._text_match._formatting_normalized_text` of the
    joined window contains the same normalization of the evidence.

    Growth is bounded, not open-ended: a match beginning somewhere inside the
    start line needs at most that line's own normalized length plus the
    evidence's, so once the window is that long the search moves on to the next
    start. That bound is exact rather than a tuned constant - a longer window
    cannot contain a match this start does not already carry - which keeps the
    whole scan linear in the substrate size for a fixed quote. The lowest,
    then shortest, matching window wins, the same tightest-span convention
    :func:`_reconcile_evidence_window` already follows.
    """
    target = _formatting_normalized_text(evidence)
    if not target:
        return None
    for start in sorted(candidates):
        limit = len(_formatting_normalized_text(candidates[start])) + len(target)
        end = start
        run: list[str] = []
        while end in candidates:
            run.append(candidates[end])
            joined = _formatting_normalized_text("\n".join(run))
            if target in joined:
                return (start, end)
            if len(joined) >= limit:
                break
            end += 1
    return None


def _file_line_count(worktree: Path, file: str) -> int | None:
    """Return *file*'s line count on disk, or ``None`` if it can't be read.

    Same degrade contract as ``_anchor_in_enclosing_def`` (#1743): a missing
    or undecodable file yields "can't tell", never a guess.
    """
    try:
        source = (worktree / file).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return len(source.splitlines())


def _line_exceeds_file_length(worktree: Path, file: str, line: int) -> bool:
    """True iff *line* is past the end of *file* as it exists under *worktree*.

    Distinguishes the two producer defects that ``invalid_line_reference``
    used to conflate: a citation that merely drifted off its true position
    (repairable, and now rescued by :func:`_content_rescue_anchor`) versus one
    naming a line the file does not have at all (#2007). A file that can't be
    read returns ``False`` — "can't tell" must never manufacture a rejection
    reason, the same opt-in, degrade-gracefully shape #1632/#1743 use.
    """
    count = _file_line_count(worktree, file)
    if count is None:
        return False
    return line > count


def _cited_lines_on_disk(worktree: Path, file: str, lines: list[int]) -> bool:
    """True iff *file* is readable under *worktree* and every line in *lines*
    is within its length (#2081).

    The positive counterpart of :func:`_line_exceeds_file_length`, and
    deliberately NOT its negation: that function's "can't tell" case (an
    unreadable file) returns ``False`` so it never manufactures an
    out-of-range rejection, and this one returns ``False`` for the same case
    so it never manufactures a rescue either. A citation earns the
    ``line_anchor_degraded`` routing only on positive proof that the cited
    line exists in the real file.
    """
    count = _file_line_count(worktree, file)
    if count is None:
        return False
    return all(line <= count for line in lines)


def _line_reference_out_of_range_detail(finding: Finding, worktree: Path) -> str:
    """Build a diagnosable ``RejectedFinding.detail`` for the out-of-range case.

    Names the file's real length alongside the cited range, so an operator can
    tell from the sentinel alone that the reviewer invented a position rather
    than merely mislocating a real one.
    """
    count = _file_line_count(worktree, finding.file)
    return (
        f"cited line_start={finding.line_start}, line_end={finding.line_end} but "
        f"{finding.file} is {count} line(s) long on disk; the evidence text was "
        "not found anywhere in the file's diff either, so no content-based "
        "re-anchoring was possible"
    )


def _evidence_removed_in_fix_diff(
    file_diffs: dict[str, str], file: str, evidence: str
) -> bool:
    """True iff *evidence* appears as a genuinely REMOVED (``-``) line in
    *file_diffs*'s raw hunk text for *file* (#2007's fix-substantiation rescue).

    Modeled on the already-shipped ``_diff_pair_rescue`` precedent for matching
    against raw hunk text rather than the structured line/window maps — but
    stricter: ``_diff_pair_rescue`` does a plain substring match against the
    whole hunk text, which cannot distinguish a removed line from an unrelated
    context line elsewhere in the same file's hunks. That distinction matters
    here specifically because a false accept in this direction — reporting a
    fix as substantiated when the cited code was never actually removed — is
    exactly what the substantiation bar exists to prevent (see
    ``_fix_is_substantiated``'s docstring: "'some line ... changed' is evidence
    a finding is anchorable, not evidence it was fixed").

    So this isolates only the ``-``-prefixed lines first, stripping just the
    marker, and normalizes both sides via :func:`_normalize_diff_text` only
    afterwards — matching markers and Unicode punctuation on the evidence side,
    never on the haystack's context or added lines.
    """
    raw = file_diffs.get(file, "")
    removed_lines = [line[1:] for line in raw.split("\n") if line.startswith("-")]
    if not removed_lines:
        return False
    removed_text = "\n".join(removed_lines)
    return _normalize_diff_text(evidence) in _normalize_diff_text(removed_text)
