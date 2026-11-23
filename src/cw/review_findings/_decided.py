"""The held-for-operator markers and their mechanical stamp (#2984, #3793).

A finding can be held for the operator instead of being fixed by the pipeline.
There are two kinds, named by :data:`HeldKind`:

- ``decision_contest`` (#2984): a reviewer that believes a recorded decision
  (the approved plan's Adopted Assumptions, Non-Goals or Decisions, or an
  operator constraint) is itself unsafe files the finding with
  :attr:`Finding.contests_decision` set to a quote of the decision plus the
  concrete harm. The fix would reverse a decision someone made on purpose, so
  a contest parks the whole verdict before any fix cycle runs.
- ``evidence_gap`` (#3793): the finding rests on a check the reviewer could
  not run (:attr:`Finding.evidence_gap` names it). An implementation change
  cannot supply the missing evidence, so only the unverifiable finding is held;
  reproduced MUST_FIX findings in the same verdict still go to repair.

In both kinds:

- a MUST_FIX is stamped ``operator_actionable``: it stops counting toward
  ``must_fix``/``blocking``;
- a SHOULD_FIX is stamped ``deferred``: a non-blocking note.

An evidence gap is effective for a MUST_FIX on any rule, and for a SHOULD_FIX
only when its rule is deferrable. A SHOULD_FIX on ``coverage.*`` or
``acceptance.*`` keeps today's route and is never turned into a non-blocking
note, or the verdict would pass (#3822).

Only a finding still at the optimistic ``fixed`` default is stamped, so an
operator's settle (``rejected``) or an earlier deferral is never overridden.
Nothing is dropped: the renderer reads the marker fields directly, so the
quote and the gap text exist once. Imports ``_models`` and the pure
``cw.models.review_engine_summary`` predicate only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.models.review_engine_summary import is_non_deferrable_rule

if TYPE_CHECKING:
    from cw.review_findings._models import (
        AcceptedFinding,
        Disposition,
        Finding,
        ReviewVerdict,
    )

HeldKind = Literal["decision_contest", "evidence_gap"]

_MUST_FIX = "MUST_FIX"
_SHOULD_FIX = "SHOULD_FIX"
_FIXED = "fixed"
_HELD: tuple[Disposition, str] = (
    "operator_actionable",
    "contests a decided constraint; held for the operator (`cw review settle`"
    " dismisses it)",
)
_NOTE: tuple[Disposition, str] = (
    "deferred",
    "contests a decided constraint; recorded as a non-blocking note",
)
_GAP_HELD: tuple[Disposition, str] = (
    "operator_actionable",
    "evidence gap: the reviewer could not verify this; held for the operator"
    " (supply the missing check and requeue the review)",
)
_GAP_NOTE: tuple[Disposition, str] = (
    "deferred",
    "evidence gap: the reviewer could not verify this; recorded as a non-blocking note",
)


def held_kind(finding: Finding) -> HeldKind | None:
    """Which hold *finding* carries, or ``None``. A contest wins over a gap."""
    if finding.contests_decision.strip():
        return "decision_contest"
    if finding.evidence_gap.strip():
        return "evidence_gap"
    return None


def held_disposition(finding: Finding) -> tuple[Disposition, str] | None:
    """The ``(disposition, detail)`` a held marker forces, or ``None``."""
    kind = held_kind(finding)
    if kind is None:
        return None
    if finding.severity == _MUST_FIX:
        return _HELD if kind == "decision_contest" else _GAP_HELD
    if finding.severity == _SHOULD_FIX:
        if kind == "decision_contest":
            return _NOTE
        if is_non_deferrable_rule(finding.rule_id):
            return None
        return _GAP_NOTE
    return None


def stamp_held_findings(verdict: ReviewVerdict) -> ReviewVerdict:
    """Stamp every still-``fixed`` held finding, then recompute the gate.

    ``must_fix`` and ``blocking`` are recomputed the way
    :func:`cw.review_finding_dispositions.suppress_adjudicated_findings` does
    after its own stamp; ``review`` counts are left untouched. A verdict with
    nothing to stamp is returned unchanged.
    """
    stamped: list[AcceptedFinding] = []
    changed = False
    for af in verdict.accepted:
        forced = held_disposition(af.finding)
        if forced is None or af.disposition != _FIXED:
            stamped.append(af)
            continue
        disposition, detail = forced
        stamped.append(
            af.model_copy(
                update={"disposition": disposition, "disposition_detail": detail}
            )
        )
        changed = True
    if not changed:
        return verdict
    must_fix = [
        af.finding
        for af in stamped
        if af.finding.severity == _MUST_FIX and af.disposition == _FIXED
    ]
    return verdict.model_copy(
        update={"accepted": stamped, "must_fix": must_fix, "blocking": bool(must_fix)}
    )


def held_findings(
    verdict: ReviewVerdict, kind: HeldKind | None = None
) -> list[AcceptedFinding]:
    """The accepted MUST_FIX findings held for the operator, optionally by *kind*."""
    return [
        af
        for af in verdict.accepted
        if af.finding.severity == _MUST_FIX
        and af.disposition == "operator_actionable"
        and (found := held_kind(af.finding)) is not None
        and (kind is None or found == kind)
    ]


def has_held_findings(verdict: ReviewVerdict, kind: HeldKind | None = None) -> bool:
    """Whether *verdict* holds any MUST_FIX finding for the operator, by *kind*."""
    return bool(held_findings(verdict, kind))


def is_gap_hold(af: AcceptedFinding) -> bool:
    """Whether *af* is an evidence-gap MUST_FIX the stamp held for the operator."""
    return (
        af.finding.severity == _MUST_FIX
        and af.disposition == "operator_actionable"
        and held_kind(af.finding) == "evidence_gap"
    )
