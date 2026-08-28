"""Adjudication → accepted-finding matching and stamping (#1805).

The core seam #1805 exists for: :func:`apply_adjudication` maps a structured
:class:`~cw.review_adjudication._models.Adjudication` list onto a verdict's
accepted findings, stamps their dispositions, and recomputes the gate-feeding
fields from the stamped result. :func:`matched_adjudications` exposes the
matching half alone, for callers that must render only the entries this seam
would actually apply.

Split out of the single ``review_adjudication.py`` module (#2011); import these
names from :mod:`cw.review_adjudication`, not from this private submodule.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.review_adjudication._models import _MUST_FIX, _OUTCOME_DISPOSITIONS
from cw.review_findings import derive_review_counts

if TYPE_CHECKING:
    from cw.review_adjudication._models import Adjudication
    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

_log = logging.getLogger(__name__)

#: ``disposition_detail`` for a finding no adjudication entry covered.
NO_ENTRY_DETAIL = "no adjudication entry recorded for this finding"


def _location_key(
    severity: str, file: str, line_start: int | None, line_end: int | None
) -> tuple[str, str, int, int]:
    """The coarse 4-field identity key shared by findings and adjudications.

    Deliberately the ``_dedup_key`` prefix minus ``evidence`` (which is a
    tiebreak here, not identity). ``None`` endpoints map to ``-1``, same as
    :func:`cw.review_findings._dedup_key`.
    """
    return (
        severity,
        file,
        line_start if line_start is not None else -1,
        line_end if line_end is not None else -1,
    )


def _entry_key(entry: Adjudication) -> tuple[str, str, int, int]:
    return _location_key(entry.severity, entry.file, entry.line_start, entry.line_end)


def _finding_key(finding: Finding) -> tuple[str, str, int, int]:
    return _location_key(
        finding.severity, finding.file, finding.line_start, finding.line_end
    )


def _candidates_for_entry(
    entry: Adjudication, accepted: list[AcceptedFinding]
) -> list[int]:
    """Indices into *accepted* whose finding shares *entry*'s location key.

    Matching runs from the entry's side, not the finding's: the collision that
    actually needs handling is "two accepted findings share one entry's
    location", and only an entry-first pass can see that both could equally
    claim the entry.
    """
    key = _entry_key(entry)
    return [i for i, af in enumerate(accepted) if _finding_key(af.finding) == key]


def _resolve_entry(
    entry: Adjudication, accepted: list[AcceptedFinding], candidates: list[int]
) -> int | None:
    """Pick the single accepted finding *entry* decides, or ``None``.

    Zero candidates → nothing to decide. One → unambiguous. Two or more → the
    ``evidence`` tiebreak, which must narrow to exactly one; if it narrows to
    zero (the entry quotes neither) or leaves several (identical evidence),
    the entry is genuinely ambiguous and is left unconsumed rather than
    guessed at — guessing is what stamps the wrong finding.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    tied = [i for i in candidates if accepted[i].finding.evidence == entry.evidence]
    return tied[0] if len(tied) == 1 else None


def _match_adjudications(
    accepted: list[AcceptedFinding], adjudications: list[Adjudication]
) -> tuple[dict[int, Adjudication], list[Adjudication]]:
    """Map accepted-finding indices to the entries that decide them.

    Returns ``(matched, unconsumed)``. Entries are walked in payload order and
    an index already claimed by an earlier entry is never re-claimed
    (first-entry-wins), so a duplicate-entry payload degrades to "the later
    entry matched nothing" rather than raising or silently re-stamping.
    """
    matched: dict[int, Adjudication] = {}
    unconsumed: list[Adjudication] = []
    for entry in adjudications:
        index = _resolve_entry(entry, accepted, _candidates_for_entry(entry, accepted))
        if index is None or index in matched:
            unconsumed.append(entry)
            continue
        matched[index] = entry
    return matched, unconsumed


def matched_adjudications(
    accepted: list[AcceptedFinding], adjudications: list[Adjudication]
) -> list[Adjudication]:
    """The subset of *adjudications* that :func:`apply_adjudication` applies.

    Callers rendering ``.cw/deferred-findings.md`` (or any other artifact
    derived from the adjudication payload) must filter through this first —
    passing the raw, unfiltered *adjudications* list to
    :func:`render_deferred_findings_md` would let an entry that matched no
    accepted finding (stale anchor, an ambiguous same-location collision, a
    shadowed duplicate — the exact cases counted in
    ``ReviewVerdict.unmatched_adjudication_count``) still appear in the
    rendered markdown as if it had been applied, even though the verdict
    itself never recorded that decision. That reintroduces this ticket's own
    "two records disagree" failure one layer down, inside the fix meant to
    remove it.
    """
    matched, _ = _match_adjudications(accepted, adjudications)
    return list(matched.values())


def _stamp(accepted: AcceptedFinding, entry: Adjudication | None) -> AcceptedFinding:
    """Return *accepted* carrying *entry*'s decision (or the no-decision state)."""
    if entry is None:
        return accepted.model_copy(
            update={"disposition": "dropped", "disposition_detail": NO_ENTRY_DETAIL}
        )
    return accepted.model_copy(
        update={
            "disposition": _OUTCOME_DISPOSITIONS[entry.outcome],
            "disposition_detail": entry.rationale,
        }
    )


def apply_adjudication(
    verdict: ReviewVerdict, adjudications: list[Adjudication]
) -> ReviewVerdict:
    """Stamp *verdict*'s accepted findings from *adjudications* and recompute.

    Recomputing is not optional garnish: a MUST_FIX the session REJECTed still
    reads ``blocking=True`` if only ``disposition`` is stamped, which is the
    same class of lie this ticket exists to remove. The rule, derived from
    ``consolidate_verdict``'s own generic rule rather than copied from
    ``_survivors_only_verdict`` (see the package docstring for why the Codex
    path's inverted ``"deferred"`` semantics do not transfer):

    - ``"fixed"``/``"rejected"``/``"deferred"``/``"operator_actionable"`` are
      all genuine recorded decisions post-adjudication — all four stop
      blocking. ``"operator_actionable"`` (#1817) stopping ``blocking`` here is
      deliberate and does not make the finding invisible: Stage 3 still exits
      ``blocked`` for it via a separate prose-level gate keyed on the
      ``ADJUDICATIONS`` entry (``blocker.reason: "review_operator_actionable"``,
      ``auto-dev-review.md`` Step 3c), exactly the way ``plan_deviation``
      already exits independently of ``ReviewVerdict.blocking``.
    - ``"dropped"`` — nobody decided — is the ONLY disposition that still
      counts toward ``must_fix``/``blocking``, erring toward gate failure over
      silent pass-through.

    ``review.deferred`` is recomputed; ``must_fix_initial``, ``should_fix``,
    ``fix_cycles_used``, ``agents_run`` and ``had_real_commit`` are preserved
    verbatim. Those are the frozen cycle-0 baseline (``auto-dev-review.md``
    Checkpoint 3a step 4 freezes them, and ``codex_fix_loop._finalize_review``
    keeps the same split) — recomputing them from a disposition-stamped list
    would silently corrupt them.

    Never raises on a per-item mismatch (only pydantic's own ``ValidationError``
    on structurally malformed input can): an unmatched entry is logged and
    counted in ``unmatched_adjudication_count`` instead.
    """
    matched, unconsumed = _match_adjudications(verdict.accepted, adjudications)
    stamped = [_stamp(af, matched.get(i)) for i, af in enumerate(verdict.accepted)]

    for entry in unconsumed:
        # Mirrors review_findings' silently-dropped-content logging convention
        # (see the stripped-escalation warning there): a mechanical alteration
        # nobody asked for is always announced.
        _log.warning(
            "auto-dev: adjudication entry did not match any accepted finding "
            "(severity=%s, file=%s, line=%s-%s, outcome=%s)",
            entry.severity,
            entry.file,
            entry.line_start,
            entry.line_end,
            entry.outcome,
        )

    must_fix = [
        af.finding
        for af in stamped
        if af.finding.severity == _MUST_FIX and af.disposition == "dropped"
    ]
    # Borrow derive_review_counts's own deferred-counting rule instead of
    # duplicating its (severity, disposition) filter here — only `.deferred`
    # is used; must_fix_initial/should_fix/agents_run from that call are
    # discarded, since those three stay pinned at the frozen cycle-0 baseline
    # (see the docstring above), never recomputed from `stamped`.
    deferred = derive_review_counts(stamped).deferred
    return verdict.model_copy(
        update={
            "accepted": stamped,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
            "review": verdict.review.model_copy(update={"deferred": deferred}),
            "unmatched_adjudication_count": len(unconsumed),
        }
    )
