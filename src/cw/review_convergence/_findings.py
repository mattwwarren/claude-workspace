"""Findings-side helpers of the converge-to-clean engine (#2787).

Turning a :class:`~cw.review_findings.ReviewVerdict` into round findings
(:func:`from_verdict`), classifying them against the ledger's settled memory
(the "new accepted" rule), and stamping resurfaced settled findings
(:func:`suppress_settled`).

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, overload

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
from cw.review_engine.route import is_non_deferrable_rule
from cw.review_finding_dispositions import debt_key
from cw.review_findings import ReviewVerdict

if TYPE_CHECKING:
    from collections.abc import Sequence

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

#: How many of a ledger's last recorded rounds count as "the final round or two"
#: for review-debt filing. Two, not one: the last round is often a confirm or a
#: delta round that re-raises only part of what the loop rounds raised, so a
#: window of one would drop real debt; a wider window would again file findings
#: a fix removed several rounds ago.
DEBT_CURRENT_ROUND_WINDOW = 2

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
                fingerprint=finding.fingerprint,
                rule_id=finding.rule_id,
            )
        )
    return findings, unread_from_verdict(verdict)


def unread_from_verdict(verdict: ReviewVerdict) -> UnreadFindings:
    """The :class:`UnreadFindings` tally of *verdict* (the one derivation).

    Reads the three fields ``consolidate_verdict`` populates: the mechanically
    rejected MUST_FIX findings, the reviewer failures that discarded MUST_FIX or
    SHOULD_FIX claims, and the roles whose ``agents_run`` record is ``failed``.
    :func:`from_verdict` returns it beside the findings; the codex-lane gate
    (#2791) calls it directly for a loop round it records with no findings.

    A sharded round (#2864) adds its unreviewed shards, and can carry one role
    as several ``failed`` records (one per shard it failed in), so
    ``failed_roles`` names each role once, in first-seen order.
    """
    return UnreadFindings(
        rejected_must_fix=len(verdict.rejected_must_fix),
        discarded_run_failures=len(verdict.run_failures_with_should_fix_discards),
        failed_roles=tuple(
            dict.fromkeys(
                record.reviewer_role
                for record in verdict.agents_run
                if record.status == "failed"
            )
        ),
        unreviewed_shards=sum(not shard.reviewed for shard in verdict.shards),
    )


def shard_tally_from_verdict(verdict: ReviewVerdict) -> tuple[int, int]:
    """``(shard_count, shards_reviewed)`` of *verdict* for its ledger round.

    An unsharded verdict (no ``shards``) is one shard, reviewed: ``(1, 1)``.
    """
    if not verdict.shards:
        return 1, 1
    return len(verdict.shards), sum(shard.reviewed for shard in verdict.shards)


def decided_finding(item: AcceptedFinding, rationale: str) -> AcceptedFinding:
    """*item* with a disposition a ledger round can record.

    MUST_FIX is a ``fix`` (a ``dropped``/``unresolved`` one too: nobody fixed
    it); SHOULD_FIX not already rejected or deferred defaults to ``deferred``
    with *rationale*, unless the adjudication boundary marked an explicit
    ``outcome: fix``. That rationale names the round's origin (the wording is
    lane-specific, so there is no default). Existing rejected/deferred
    dispositions and non-gating NIT, PRINCIPLE, and DEBT findings pass through.
    """
    severity = item.finding.severity
    if (
        severity == "SHOULD_FIX"
        and item.disposition not in ("rejected", "deferred")
        and not item.adjudicated_fix
    ):
        return item.model_copy(
            update={"disposition": "deferred", "disposition_detail": rationale}
        )
    if severity == "MUST_FIX" and item.disposition in ("dropped", "unresolved"):
        return item.model_copy(update={"disposition": "fixed"})
    return item


def decide_verdict(verdict: ReviewVerdict, rationale: str) -> ReviewVerdict:
    """*verdict* with every ``accepted`` finding passed through :func:`decided_finding`.

    Only ``accepted`` is rebuilt; the frozen counts, ``rejected_must_fix`` and
    ``agents_run`` are untouched, so the R29 unread tally is unchanged.
    """
    return verdict.model_copy(
        update={
            "accepted": [decided_finding(item, rationale) for item in verdict.accepted]
        }
    )


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


def settled_applies(
    entry: SettledEntry,
    severity: GatingSeverity,
    fingerprint: str | None,
    rule_id: str | None,
) -> bool:
    """Whether a settled *entry* silences a finding re-raised with these fields.

    A builtin identity (no fingerprint) always applies, so builtin suppression
    is unchanged. For an engine identity (RFC 0017 R13) the entry applies only
    when it is a ``defer``, the re-raise is not an escalation (a MUST_FIX over a
    SHOULD_FIX defer is new work), and the rule is not a coverage or acceptance
    rule. Otherwise the finding counts as new.
    """
    if fingerprint is None:
        return True
    if entry.outcome != "defer":
        return False
    if severity == "MUST_FIX" and entry.identity.severity != "MUST_FIX":
        return False
    return not is_non_deferrable_rule(rule_id)


def operator_defer_fingerprints(ledger: ConvergenceLedger) -> frozenset[str]:
    """Fingerprints an operator deferred at MUST_FIX severity (#3039, R15).

    These let :func:`cw.review_engine.route.engine_route` accept a MUST_FIX
    ``defer`` row for the same fingerprint.
    """
    return frozenset(
        entry.identity.fingerprint
        for entry in ledger.settled
        if entry.origin == "operator"
        and entry.outcome == "defer"
        and entry.identity.severity == "MUST_FIX"
        and entry.identity.fingerprint is not None
    )


@dataclass(frozen=True)
class Classified:
    """A round's findings sorted by the new-accepted rule."""

    new_fix: list[RoundFinding]
    new_operator: list[RoundFinding]
    newly_settled: list[SettledEntry]
    resurfaced: int
    #: Identities of the settled entries this round re-raised, in first-seen order.
    reseen: list[FindingIdentity] = field(default_factory=list)

    @property
    def new_accepted(self) -> int:
        """Count of findings that keep the loop open."""
        return len(self.new_fix) + len(self.new_operator)


def settled_debt_key(outcome: str, file: str, summary: str) -> str | None:
    """The debt join key a settled entry stores (#3094), or None off ``defer``.

    The built-in shape (``fingerprint=None``), the same key filing computes from
    the verbatim summary. Only a ``defer`` owes a debt ticket, so a ``reject``
    stores none.
    """
    if outcome != "defer":
        return None
    return debt_key(file, summary)


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
    reseen: dict[FindingIdentity, None] = {}
    resurfaced = 0
    for finding in findings:
        identity = finding_identity(
            finding.severity,
            finding.file,
            finding.summary,
            fingerprint=finding.fingerprint,
        )
        entry = settled.get(identity)
        if entry is not None and settled_applies(
            entry, finding.severity, finding.fingerprint, finding.rule_id
        ):
            resurfaced += 1
            reseen[identity] = None
        elif finding.outcome == "fix":
            new_fix.append(finding)
        elif finding.outcome == "operator_action":
            new_operator.append(finding)
        elif identity not in newly_settled:
            outcome_value: Literal["reject", "defer"] = (
                "reject" if finding.outcome == "reject" else "defer"
            )
            newly_settled[identity] = SettledEntry(
                identity=identity,
                outcome=outcome_value,
                rationale=finding.rationale,
                round=round_number,
                debt_key=settled_debt_key(outcome_value, finding.file, finding.summary),
                last_seen_round=round_number,
            )
    return Classified(
        new_fix,
        new_operator,
        list(newly_settled.values()),
        resurfaced,
        list(reseen),
    )


def mark_reseen(
    settled: Sequence[SettledEntry],
    reseen: Sequence[FindingIdentity],
    round_number: int,
) -> list[SettledEntry]:
    """*settled* with each *reseen* identity's ``last_seen_round`` moved forward.

    Entries not re-raised this round are returned as they are, so a legacy
    ``None`` stays "unknown" until a round raises it again.
    """
    raised = set(reseen)
    return [
        entry.model_copy(update={"last_seen_round": round_number})
        if entry.identity in raised
        else entry
        for entry in settled
    ]


def stale_defer_keys(ledger: ConvergenceLedger) -> frozenset[str]:
    """Debt keys of settled defers no recent recorded round raised (#3137 follow-up).

    A defer is stale when its ``last_seen_round`` is known and falls more than
    :data:`DEBT_CURRENT_ROUND_WINDOW` rounds behind the ledger's last recorded
    round, so only the final round or two decide. An unknown ``last_seen_round``
    and a defer with no ``debt_key`` (an engine fingerprint finding) are never
    stale: the filter errs toward filing.
    """
    cutoff = len(ledger.rounds) - DEBT_CURRENT_ROUND_WINDOW
    return frozenset(
        entry.debt_key
        for entry in ledger.settled
        if entry.outcome == "defer"
        and entry.debt_key is not None
        and entry.last_seen_round is not None
        and entry.last_seen_round <= cutoff
    )


def _adjudication_for(
    entry: SettledEntry,
    *,
    severity: GatingSeverity,
    file: str,
    summary: str,
    line_start: int | None = None,
    line_end: int | None = None,
    evidence: str = "",
    fingerprint: str | None = None,
    rule_id: str | None = None,
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
        fingerprint=fingerprint,
        non_deferrable=is_non_deferrable_rule(rule_id),
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
            finding_identity(
                finding.severity,
                finding.file,
                finding.summary,
                fingerprint=finding.fingerprint,
            )
        )
        if entry is None or not settled_applies(
            entry, finding.severity, finding.fingerprint, finding.rule_id
        ):
            stamped.append(finding)
            continue
        adjudications.append(
            _adjudication_for(
                entry,
                severity=finding.severity,
                file=finding.file,
                summary=finding.summary,
                fingerprint=finding.fingerprint,
                rule_id=finding.rule_id,
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
        finding = accepted.finding
        entry = settled.get(
            finding_identity(
                severity,
                finding.file,
                finding.summary,
                fingerprint=finding.fingerprint,
            )
        )
        if entry is not None and settled_applies(
            entry, severity, finding.fingerprint, finding.rule_id
        ):
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
                fingerprint=finding.fingerprint,
                rule_id=finding.rule_id,
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
