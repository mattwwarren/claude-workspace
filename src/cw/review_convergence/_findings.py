"""Findings-side helpers of the converge-to-clean engine (#2787).

Turning a :class:`~cw.review_findings.ReviewVerdict` into round findings
(:func:`from_verdict`), classifying them against the ledger's settled memory
(the "new accepted" rule), and stamping resurfaced settled findings
(:func:`suppress_settled`).

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, overload

from cw.review_adjudication import Adjudication, AdjudicationOutcome
from cw.review_convergence._identity import FindingIdentity, finding_identity
from cw.review_convergence._models import (
    ConvergenceError,
    FrozenCounts,
    GatingSeverity,
    RoundFinding,
    RoundInput,
    SettledEntry,
    UnreadFindings,
)
from cw.review_findings import ReviewVerdict

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger
    from cw.review_findings import AcceptedFinding, Disposition, Severity

#: ``AcceptedFinding.disposition`` -> the adjudication outcome a round records.
#: ``dropped`` (nobody decided) and ``unresolved`` (a codex fix-loop survivor)
#: are absent on purpose: a round cannot be recorded over an undecided finding.
_DISPOSITION_OUTCOME: dict[str, AdjudicationOutcome] = {
    "fixed": "fix",
    "rejected": "reject",
    "deferred": "defer",
    "operator_actionable": "operator_action",
}

#: Rationale used on the ``Adjudication`` of a settled entry that stored none:
#: ``Adjudication`` refuses a blank rationale for a non-fix outcome.
_NO_RATIONALE = "settled earlier in this review (no rationale recorded)"


def gating_severity(severity: Severity) -> GatingSeverity | None:
    """*severity* narrowed to the two that gate a round, else ``None``."""
    if severity == "MUST_FIX":
        return "MUST_FIX"
    if severity == "SHOULD_FIX":
        return "SHOULD_FIX"
    return None


def from_verdict(verdict: ReviewVerdict) -> tuple[list[RoundFinding], UnreadFindings]:
    """Round findings of *verdict*, plus what the verdict lost unread (R29).

    Keeps only MUST_FIX and SHOULD_FIX accepted findings (NIT, PRINCIPLE and
    DEBT never count) and maps ``disposition`` to the adjudication outcome. A
    finding nobody decided (``dropped``) or a fix-loop ``unresolved`` survivor
    raises :class:`ConvergenceError`. The :class:`UnreadFindings` it returns
    reads three fields ``consolidate_verdict`` populates: the mechanically
    rejected MUST_FIX findings, the reviewer failures that discarded MUST_FIX or
    SHOULD_FIX claims, and the roles whose ``agents_run`` record is ``failed``.
    """
    findings: list[RoundFinding] = []
    for accepted in verdict.accepted:
        finding = accepted.finding
        severity = gating_severity(finding.severity)
        if severity is None:
            continue
        outcome = _DISPOSITION_OUTCOME.get(accepted.disposition)
        if outcome is None:
            msg = (
                f"undecided finding: {finding.file}: {finding.summary!r} has "
                f"disposition {accepted.disposition!r}; adjudicate it before "
                "recording the round"
            )
            raise ConvergenceError(msg)
        findings.append(
            RoundFinding(
                severity=severity,
                file=finding.file,
                summary=finding.summary,
                outcome=outcome,
                rationale=accepted.disposition_detail,
            )
        )
    unread = UnreadFindings(
        rejected_must_fix=len(verdict.rejected_must_fix),
        discarded_run_failures=len(verdict.run_failures_with_should_fix_discards),
        failed_roles=tuple(
            record.reviewer_role
            for record in verdict.agents_run
            if record.status == "failed"
        ),
    )
    return findings, unread


def round_input_findings(
    payload: RoundInput,
) -> tuple[list[RoundFinding], UnreadFindings | None]:
    """The findings of a ``record-round`` envelope, and its unread tally.

    A verdict envelope goes through :func:`from_verdict` and carries its
    :class:`UnreadFindings`; the findings form carries no verdict and so no
    tally (``None``).
    """
    if payload.verdict is not None:
        return from_verdict(payload.verdict)
    return list(payload.findings or []), None


def frozen_counts_from_verdict(verdict: ReviewVerdict) -> FrozenCounts:
    """The cycle-0 counts of *verdict*, for the ledger's frozen baseline."""
    return FrozenCounts(
        must_fix_initial=verdict.review.must_fix_initial,
        should_fix=verdict.review.should_fix,
        agents_run=verdict.review.agents_run,
        rejected_count=verdict.rejected_count,
        rejected_count_by_severity=dict(verdict.rejected_count_by_severity),
    )


def settled_index(ledger: ConvergenceLedger) -> dict[FindingIdentity, SettledEntry]:
    """The ledger's settled memory keyed by identity (either origin)."""
    return {entry.identity: entry for entry in ledger.settled}


@dataclass(frozen=True)
class Classified:
    """A round's findings sorted by the new-accepted rule."""

    new_fix: list[RoundFinding]
    new_operator: list[RoundFinding]
    newly_settled: list[SettledEntry]
    resurfaced: int

    @property
    def new_accepted(self) -> int:
        """Count of findings that keep the loop open."""
        return len(self.new_fix) + len(self.new_operator)


def classify_findings(
    ledger: ConvergenceLedger, findings: list[RoundFinding], round_number: int
) -> Classified:
    """Apply the new-accepted rule to *findings* (adopted assumption).

    A finding is new-accepted when its outcome is ``fix`` or ``operator_action``
    and its identity is not already settled: "settled" means present in
    ``ledger.settled`` with EITHER origin, whatever outcome the finding arrived
    stamped with, so a prose step that skipped ``suppress`` cannot defeat an
    operator settle. A reject or defer of an unseen identity is recorded as
    newly settled and does not count.
    """
    settled = settled_index(ledger)
    new_fix: list[RoundFinding] = []
    new_operator: list[RoundFinding] = []
    newly_settled: dict[FindingIdentity, SettledEntry] = {}
    resurfaced = 0
    for finding in findings:
        identity = finding_identity(finding.severity, finding.file, finding.summary)
        if identity in settled:
            resurfaced += 1
        elif finding.outcome == "fix":
            new_fix.append(finding)
        elif finding.outcome == "operator_action":
            new_operator.append(finding)
        elif identity not in newly_settled:
            newly_settled[identity] = SettledEntry(
                identity=identity,
                outcome="reject" if finding.outcome == "reject" else "defer",
                rationale=finding.rationale,
                round=round_number,
            )
    return Classified(new_fix, new_operator, list(newly_settled.values()), resurfaced)


def _adjudication_for(
    entry: SettledEntry,
    *,
    severity: GatingSeverity,
    file: str,
    summary: str,
    line_start: int | None = None,
    line_end: int | None = None,
    evidence: str = "",
) -> Adjudication:
    """The ``Adjudication`` a suppression contributes: stored outcome and rationale."""
    return Adjudication(
        severity=severity,
        file=file,
        line_start=line_start,
        line_end=line_end,
        evidence=evidence,
        summary=summary,
        outcome=entry.outcome,
        rationale=entry.rationale or _NO_RATIONALE,
    )


@overload
def suppress_settled(
    ledger: ConvergenceLedger, target: ReviewVerdict
) -> tuple[ReviewVerdict, list[Adjudication]]: ...


@overload
def suppress_settled(
    ledger: ConvergenceLedger, target: list[RoundFinding]
) -> tuple[list[RoundFinding], list[Adjudication]]: ...


def suppress_settled(
    ledger: ConvergenceLedger, target: ReviewVerdict | list[RoundFinding]
) -> tuple[ReviewVerdict | list[RoundFinding], list[Adjudication]]:
    """Stamp every resurfaced settled finding in *target* with its stored outcome.

    Works on a :class:`ReviewVerdict` or a list of :class:`RoundFinding`. A
    finding whose identity is settled (adjudication or operator origin, treated
    identically) is STAMPED, not removed: it keeps its place and gets outcome
    ``reject``/``defer`` (verdict form: disposition ``rejected``/``deferred``)
    with the stored rationale verbatim, and the call returns one
    :class:`Adjudication` per stamped finding carrying that same rationale, so
    ``merge_deferred_adjudications`` dedupes them. Nothing resurfaced returns
    *target* unchanged and no adjudications.

    The verdict form rebuilds ``accepted``, ``must_fix`` and ``blocking`` only
    (``model_copy(update=...)``, exactly like ``cw.review_adjudication._voided
    .apply_voided_suppression``), so the frozen counts, ``rejected_must_fix``,
    ``run_failures_with_should_fix_discards`` and ``agents_run`` are untouched.
    """
    if isinstance(target, ReviewVerdict):
        return _suppress_verdict(ledger, target)
    return _suppress_findings(ledger, target)


def _suppress_findings(
    ledger: ConvergenceLedger, findings: list[RoundFinding]
) -> tuple[list[RoundFinding], list[Adjudication]]:
    settled = settled_index(ledger)
    stamped: list[RoundFinding] = []
    adjudications: list[Adjudication] = []
    for finding in findings:
        entry = settled.get(
            finding_identity(finding.severity, finding.file, finding.summary)
        )
        if entry is None:
            stamped.append(finding)
            continue
        adjudications.append(
            _adjudication_for(
                entry,
                severity=finding.severity,
                file=finding.file,
                summary=finding.summary,
            )
        )
        stamped.append(
            finding.model_copy(
                update={"outcome": entry.outcome, "rationale": entry.rationale}
            )
        )
    return stamped, adjudications


def _suppress_verdict(
    ledger: ConvergenceLedger, verdict: ReviewVerdict
) -> tuple[ReviewVerdict, list[Adjudication]]:
    settled = settled_index(ledger)
    matches: dict[int, tuple[SettledEntry, GatingSeverity]] = {}
    for index, accepted in enumerate(verdict.accepted):
        severity = gating_severity(accepted.finding.severity)
        if severity is None:
            continue
        entry = settled.get(
            finding_identity(severity, accepted.finding.file, accepted.finding.summary)
        )
        if entry is not None:
            matches[index] = (entry, severity)
    if not matches:
        return verdict, []

    stamped: list[AcceptedFinding] = []
    adjudications: list[Adjudication] = []
    for index, accepted in enumerate(verdict.accepted):
        match = matches.get(index)
        if match is None:
            stamped.append(accepted)
            continue
        entry, severity = match
        finding = accepted.finding
        adjudications.append(
            _adjudication_for(
                entry,
                severity=severity,
                file=finding.file,
                summary=finding.summary,
                line_start=finding.line_start,
                line_end=finding.line_end,
                evidence=finding.evidence,
            )
        )
        disposition: Disposition = (
            "rejected" if entry.outcome == "reject" else "deferred"
        )
        stamped.append(
            accepted.model_copy(
                update={
                    "disposition": disposition,
                    "disposition_detail": entry.rationale,
                }
            )
        )
    must_fix = [
        accepted.finding
        for index, accepted in enumerate(stamped)
        if accepted.finding.severity == "MUST_FIX"
        and index not in matches
        and accepted.disposition != "deferred"
    ]
    suppressed = verdict.model_copy(
        update={"accepted": stamped, "must_fix": must_fix, "blocking": bool(must_fix)}
    )
    return suppressed, adjudications
