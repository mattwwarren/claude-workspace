"""The decided-constraint contest marker and its mechanical stamp (#2984).

A reviewer that believes a recorded decision (the approved plan's Adopted
Assumptions, Non-Goals or Decisions, or an operator constraint) is itself
unsafe files the finding with :attr:`Finding.contests_decision` set to a quote
of the decision plus the concrete harm. Such a finding must reach the operator
and must never be fixed by the pipeline, because the fix would reverse a
decision someone made on purpose:

- a MUST_FIX is stamped ``operator_actionable``: it stops counting toward
  ``must_fix``/``blocking``, and the codex synthesis parks the whole verdict
  with ``review_operator_actionable`` before any fix cycle runs;
- a SHOULD_FIX is stamped ``deferred``: a non-blocking note.

Only a finding still at the optimistic ``fixed`` default is stamped, so an
operator's settle (``rejected``) or an earlier deferral is never overridden.
The quote is not copied into ``disposition_detail``: the renderer reads
``contests_decision`` directly, so it exists once. Imports ``_models`` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.review_findings._models import (
        AcceptedFinding,
        Disposition,
        Finding,
        ReviewVerdict,
    )

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


def contested_disposition(finding: Finding) -> tuple[Disposition, str] | None:
    """The ``(disposition, detail)`` a contest marker forces, or ``None``."""
    if not finding.contests_decision.strip():
        return None
    if finding.severity == _MUST_FIX:
        return _HELD
    if finding.severity == _SHOULD_FIX:
        return _NOTE
    return None


def stamp_decision_contests(verdict: ReviewVerdict) -> ReviewVerdict:
    """Stamp every still-``fixed`` marked finding, then recompute the gate.

    ``must_fix`` and ``blocking`` are recomputed the way
    :func:`cw.review_finding_dispositions.suppress_adjudicated_findings` does
    after its own stamp; ``review`` counts are left untouched. A verdict with
    nothing to stamp is returned unchanged.
    """
    stamped: list[AcceptedFinding] = []
    changed = False
    for af in verdict.accepted:
        forced = contested_disposition(af.finding)
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


def contested_findings(verdict: ReviewVerdict) -> list[AcceptedFinding]:
    """The accepted MUST_FIX findings held for the operator by a contest."""
    return [
        af
        for af in verdict.accepted
        if af.finding.severity == _MUST_FIX
        and af.disposition == "operator_actionable"
        and af.finding.contests_decision.strip()
    ]


def has_contested_decision(verdict: ReviewVerdict) -> bool:
    """Whether *verdict* holds any contest-stamped MUST_FIX finding."""
    return bool(contested_findings(verdict))
