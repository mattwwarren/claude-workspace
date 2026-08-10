"""Claude-native review adjudication seam (#1805).

``cw review consolidate`` leaves every surviving :class:`AcceptedFinding` at
``disposition="fixed"`` — an optimistic default, correct at the moment it is
written because nothing has been adjudicated yet. The Codex fix-loop adapter
overwrites it from its own re-review. The Claude-native ``/auto-dev-review``
pipeline had no equivalent overwrite: Checkpoint 3a's FIX / REJECT / DEFER
adjudication happened entirely in LLM prose, and ``.cw/deferred-findings.md``
was hand-authored from that same prose — two independently typed records of
one judgment, only one of which was accurate.

This module is that missing seam. :func:`apply_adjudication` stamps the
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
opposite of this module's: there it means "the fix loop capped out, this
MUST_FIX is still genuinely unresolved" (so ``blocking`` must stay ``True``
for a deferred survivor, which is exactly why that function computes
``blocking`` from the open-finding set rather than from dispositions). Here
``"deferred"`` means "the coordinating session deliberately decided this may
ship un-fixed" — so a deferred finding correctly stops blocking. The two
shapes must NOT be unified by generalizing one to cover the other. If a
second consumer ever needs *this* seam (not merely the ``Disposition`` /
:class:`AcceptedFinding` types, which are shared already), that is the trigger
to write an ADR; until then this docstring is the record of why they differ.

Public surface: :class:`Adjudication`, :data:`AdjudicationOutcome`,
:func:`apply_adjudication`, :func:`verify_fixed_dispositions`,
:func:`render_deferred_findings_md`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, model_validator

from cw.review_findings import (
    AcceptedFinding,
    Disposition,
    Severity,
    _line_reference_valid,
)

if TYPE_CHECKING:
    from cw.review_findings import CapturedDiff, Finding, ReviewVerdict

_log = logging.getLogger(__name__)

AdjudicationOutcome = Literal["fix", "reject", "defer"]

# The three genuine, recorded post-adjudication decisions. Every other
# disposition an accepted finding can end at is an absence of decision.
_OUTCOME_DISPOSITIONS: dict[AdjudicationOutcome, Disposition] = {
    "fix": "fixed",
    "reject": "rejected",
    "defer": "deferred",
}

_MUST_FIX = "MUST_FIX"
# The two severities that feed the gate aggregates; NIT/PRINCIPLE never do
# (mirrors `derive_review_counts`'s own filter).
_GATE_SEVERITIES = (_MUST_FIX, "SHOULD_FIX")

#: ``disposition_detail`` for a finding no adjudication entry covered.
NO_ENTRY_DETAIL = "no adjudication entry recorded for this finding"

_DEFERRED_MD_TITLE = "# Deferred Review Findings"
# One line in the rendered artifact; split here only to stay inside the
# 88-column lint limit.
_DEFERRED_MD_PROVENANCE = (
    "<!-- written by Stage 3 (auto-dev-review.md), consumed by "
    "Stage 4 Step 4d (auto-dev-finalize.md) -->"
)
_DEFERRED_MD_HEADING = "## Review adjudication"
_REJECTED_MD_HEADING = "Rejected (intentional / documented tradeoff):"
_DEFERRED_SENTINEL = "DEFERRED-REVIEW-FINDINGS"


class Adjudication(BaseModel):
    """One coordinating-session decision about one accepted finding.

    ``severity``/``file``/``line_start``/``line_end`` are the identity fields
    matched against :attr:`AcceptedFinding.finding` (see
    :func:`_location_key`). ``reviewers`` is deliberately NOT among them: it is
    the post-dedup merged role list, so it carries no disambiguating power
    beyond what the location key already has.

    ``evidence`` is a same-location tiebreak, not part of the identity key —
    the fragility this ticket exists to fix is precisely verbatim
    evidence-string and off-by-one line mismatches (#1792, #1738, #1715,
    #1743), so requiring an exact evidence match to identify a finding
    reproduces the bug. It still disambiguates the rare case of two distinct
    findings at one location.

    ``summary`` is render-only: :func:`render_deferred_findings_md` fills the
    documented ``"<summary>"`` slot from it, so rendering needs no second
    lookup against the verdict.
    """

    severity: Severity
    file: str
    line_start: int | None = None
    line_end: int | None = None
    evidence: str = ""
    summary: str = ""
    outcome: AdjudicationOutcome
    rationale: str = ""

    @model_validator(mode="after")
    def _check_rationale_recorded(self) -> Adjudication:
        # "Record the rationale" is the adjudication instruction's own rule for
        # REJECT/DEFER (auto-dev-review.md Checkpoint 3a) — a blank one is a
        # contract violation, not a silently-accepted value. A FIX needs none:
        # the fix itself is the record.
        if self.outcome != "fix" and not self.rationale.strip():
            msg = (
                f"an adjudication with outcome={self.outcome!r} must record a "
                "non-empty rationale"
            )
            raise ValueError(msg)
        return self


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
    ``_survivors_only_verdict`` (see the module docstring for why the Codex
    path's inverted ``"deferred"`` semantics do not transfer):

    - ``"fixed"``/``"rejected"``/``"deferred"`` are all genuine recorded
      decisions post-adjudication — all three stop blocking.
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
    deferred = sum(
        1
        for af in stamped
        if af.disposition == "deferred" and af.finding.severity in _GATE_SEVERITIES
    )
    return verdict.model_copy(
        update={
            "accepted": stamped,
            "must_fix": must_fix,
            "blocking": bool(must_fix),
            "review": verdict.review.model_copy(update={"deferred": deferred}),
            "unmatched_adjudication_count": len(unconsumed),
        }
    )


def _fix_is_substantiated(finding: Finding, fix_diff: CapturedDiff) -> bool:
    """True iff *fix_diff* actually touches *finding*'s cited location.

    A file-level finding (both endpoints ``None``) needs only the file to
    appear in the diff. A line-anchored one goes through
    ``_line_reference_valid`` — the same tolerance-aware gate consolidation
    uses, so a fix landing a line or two off its cited anchor is not called a
    non-fix. ``worktree=None`` deliberately disables the #1743 enclosing-def
    fallback: "some line inside the enclosing function changed" is evidence a
    finding is *anchorable*, not evidence it was *fixed*.
    """
    if finding.line_start is None and finding.line_end is None:
        return finding.file in fix_diff.files
    return _line_reference_valid(fix_diff, finding, worktree=None)


def verify_fixed_dispositions(
    verdict: ReviewVerdict, fix_diff: CapturedDiff
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
    """
    accepted: list[AcceptedFinding] = []
    for af in verdict.accepted:
        if af.disposition != "fixed" or _fix_is_substantiated(af.finding, fix_diff):
            accepted.append(af)
            continue
        _log.warning(
            "auto-dev: downgraded 'fixed' disposition — fix-cycle diff does "
            "not touch cited location (file=%s, line=%s)",
            af.finding.file,
            af.finding.line_start,
        )
        accepted.append(
            af.model_copy(
                update={
                    "disposition": "dropped",
                    "disposition_detail": (
                        "fixed disposition claimed but fix-cycle diff does not "
                        f"touch {af.finding.file}:{af.finding.line_start}"
                    ),
                }
            )
        )
    return verdict.model_copy(update={"accepted": accepted})


def render_deferred_findings_md(adjudications: list[Adjudication]) -> str:
    """Render *adjudications* into the ``.cw/deferred-findings.md`` artifact.

    Reproduces the block documented in ``.claude/commands/auto-dev-review.md``
    byte-for-byte — Stage 4 Step 4d copies it into the PR body verbatim and the
    PR-hygiene sweep greps the ``DEFERRED-REVIEW-FINDINGS`` sentinels
    literally, so the shape is a contract, not a style choice.

    Returns ``""`` when there is nothing to record (every finding was fixed):
    the documented rule is "omit the file entirely", so the caller skips the
    write rather than leaving an empty artifact behind.
    """
    rejected = [a for a in adjudications if a.outcome == "reject"]
    deferred = [a for a in adjudications if a.outcome == "defer"]
    if not rejected and not deferred:
        return ""

    sections = [
        f"{_DEFERRED_MD_TITLE}\n{_DEFERRED_MD_PROVENANCE}",
        _DEFERRED_MD_HEADING,
    ]
    if rejected:
        rows = "\n".join(
            f'- {a.file} — "{a.summary}" — {a.rationale}' for a in rejected
        )
        sections.append(f"{_REJECTED_MD_HEADING}\n{rows}")
    if deferred:
        entries = "\n".join(
            f"- severity: {a.severity}\n"
            f'  summary: "{a.summary}"\n'
            f"  file: {a.file}\n"
            f'  rationale: "{a.rationale}"'
            for a in deferred
        )
        sections.append(
            f"<!-- {_DEFERRED_SENTINEL}\n{entries}\n{_DEFERRED_SENTINEL} -->"
        )
    return "\n\n".join(sections) + "\n"
