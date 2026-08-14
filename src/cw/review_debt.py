"""Debt fingerprinting, promotion, and dedup for the review pipeline (#1837).

The fix loop re-reviews a delta each cycle instead of the whole PR, so a
finding it declines to act on — an accepted DEBT-severity finding, or a
MUST_FIX the admission gate refuses as treadmill — must still be recorded
somewhere durable. This module owns that translation: a stable semantic
fingerprint for finding identity, promotion of a finding into a
:class:`~cw.review_findings.DebtRecord`, and first-discovered-wins dedup.

Lives here rather than in :mod:`cw.review_findings` (which owns the
:class:`~cw.review_findings.DebtRecord` model itself) for the same reason
:mod:`cw.review_adjudication` does: a one-directional import from the model
module keeps the two decoupled, and ``review_findings`` is already well past
the repo's module-size ceiling.

The fingerprint is deliberately NOT
:func:`cw.review_findings._dedup_key` (positional and evidence-exact, so any
line drift or reworded evidence mints a new identity) nor
``review_adjudication._voided_fingerprint`` (severity- and evidence-keyed).
Both encode "severity is part of identity", which is exactly wrong here: the
same problem downgraded from MUST_FIX to DEBT is still the same problem.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from cw.review_findings import FINGERPRINT_VERSION, DebtRecord

if TYPE_CHECKING:
    from cw.review_findings import AcceptedFinding

_log = logging.getLogger(__name__)

# The file value #1817 requires on a finding with no diff anchor at all. Such
# a finding has no stable path to key by, so it is never fingerprinted.
_NO_ANCHOR_FILE = "N/A"

_WHITESPACE_RE = re.compile(r"\s+")
# Positional references, stripped so the same finding re-raised after the code
# moved keeps one identity. The optional leading " at" is part of the match so
# "…at line 42" and "…:42" normalize to the same text rather than differing by
# a stranded preposition.
_POSITION_RE = re.compile(r"(?:\s+at)?\s*(?::\d+\b|\blines?\s+\d+(?:\s*-\s*\d+)?)")
# Every surviving digit run collapses to one placeholder: "3 call sites" and
# "4 call sites" are the same finding counted at two moments.
_DIGIT_RUN_RE = re.compile(r"\d+")
_DIGIT_PLACEHOLDER = "N"


def _normalize_summary(summary: str) -> str:
    """Reduce a finding summary to its position- and count-independent form.

    Four transforms, in order: case fold, collapse whitespace runs, strip
    position patterns, replace remaining digit runs with a placeholder.
    Backticked identifiers are preserved verbatim apart from the shared case
    fold — they are not exempted from it, just never fuzzy-matched further.

    Exact match after normalization is the whole contract: there is no
    stemming, no synonym table, no edit-distance. Two genuinely different
    findings that happen to normalize alike WILL false-merge, which
    :func:`dedupe_debt` logs rather than tries to prevent.
    """
    collapsed = _WHITESPACE_RE.sub(" ", summary.lower()).strip()
    without_positions = _POSITION_RE.sub("", collapsed)
    masked = _DIGIT_RUN_RE.sub(_DIGIT_PLACEHOLDER, without_positions)
    return _WHITESPACE_RE.sub(" ", masked).strip()


def fingerprint_v1(file: str, summary: str) -> tuple[str, str] | None:
    """Return the stable ``(file, normalized_summary)`` identity for a finding.

    Takes no severity argument by design: the same problem reported as
    MUST_FIX in one cycle and DEBT in the next is one problem, and a
    severity-keyed identity would silently double-count it.

    Returns ``None`` for a ``file="N/A"`` finding (#1817's no-diff-anchor
    case): there is no real path to key on, so it gets no cross-cycle memory
    and no debt record.
    """
    if file == _NO_ANCHOR_FILE:
        return None
    return (file, _normalize_summary(summary))


def promote_debt_finding(
    af: AcceptedFinding, *, discovery_sha: str
) -> DebtRecord | None:
    """Build the :class:`DebtRecord` for an accepted finding, or ``None``.

    ``None`` when the finding cannot be fingerprinted — see
    :func:`fingerprint_v1`. Severity-agnostic on purpose: both an accepted
    DEBT finding and a MUST_FIX the admission gate refused arrive here.
    """
    fingerprint = fingerprint_v1(af.finding.file, af.finding.summary)
    if fingerprint is None:
        return None
    return DebtRecord(
        fingerprint=fingerprint,
        fingerprint_version=FINGERPRINT_VERSION,
        file=af.finding.file,
        evidence=af.finding.evidence,
        summary=af.finding.summary,
        suggested_follow_up=af.finding.suggested_fix,
        discovery_sha=discovery_sha,
        reviewer_role=af.reviewers[0],
    )


def record_debt(ledger: dict[tuple[str, str], DebtRecord], record: DebtRecord) -> None:
    """Insert *record* into *ledger* first-discovered-wins, in place.

    A second record under an existing fingerprint is dropped, keeping the
    earliest ``discovery_sha``. When the two raw summaries differ, that drop
    is a possible false merge, so it is logged once with both summaries named
    — the accepted cost of exact-match-only normalization.
    """
    existing = ledger.get(record.fingerprint)
    if existing is None:
        ledger[record.fingerprint] = record
        return
    if existing.summary != record.summary:
        _log.warning(
            "review debt: two findings in %s share fingerprint %r but have "
            "different summaries (%r vs %r) — keeping the first; this may be "
            "a false merge from summary normalization",
            record.file,
            record.fingerprint,
            existing.summary,
            record.summary,
        )


def dedupe_debt(records: list[DebtRecord]) -> list[DebtRecord]:
    """Collapse *records* by fingerprint, first-discovered-wins."""
    ledger: dict[tuple[str, str], DebtRecord] = {}
    for record in records:
        record_debt(ledger, record)
    return list(ledger.values())
