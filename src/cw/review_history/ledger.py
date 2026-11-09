"""The ledger path of :mod:`cw.review_history`: rounds and settled entries (#2790).

Pure. Builds the round views, settled views and summary from a converge
ledger and the per-round verdict files already read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models.review_history import (
    HistorySummary,
    OverrideView,
    ReleaseView,
    RoundView,
    SettledView,
    SeveritySplit,
)
from cw.review_convergence import (
    LEDGER_STATUS_CONVERGED,
    ConvergenceError,
    classify_findings,
    from_verdict,
)
from cw.review_history.inputs import HistoryInputs, _Part, _series

if TYPE_CHECKING:
    from cw.review_convergence import (
        ConfirmOverride,
        ConvergenceLedger,
        RoundRecord,
        SettledEntry,
    )
    from cw.review_findings import ReviewVerdict


def _ledger_part(
    ledger: ConvergenceLedger, inputs: HistoryInputs, notes: list[str]
) -> _Part:
    settled = [_settled_view(entry) for entry in ledger.settled]
    numbers = {record.round for record in ledger.rounds}
    by_round: dict[int, list[SettledView]] = {}
    for view, entry in zip(settled, ledger.settled, strict=True):
        if view.origin != "adjudication":
            continue
        if view.round in numbers:
            by_round.setdefault(view.round, []).append(view)
        else:
            notes.append(
                f"settled entry {entry.identity.audit_key()} names round "
                f"{view.round}, which has no recorded round; listed under "
                "settled only"
            )
    overrides = {o.round: _override_view(o) for o in ledger.confirm_overrides}
    rounds = [
        _ledger_round_view(
            record,
            by_round.get(record.round, []),
            _severity_split(ledger, record, inputs.verdicts.get(record.round), notes),
            overrides.get(record.round),
        )
        for record in ledger.rounds
    ]
    if inputs.missing_verdict_rounds:
        listed = ", ".join(str(n) for n in inputs.missing_verdict_rounds)
        notes.append(
            f"round(s) {listed}: no per-round verdict file (a findings-form round, "
            "or the file was removed); severity split not recorded"
        )
    if not ledger.rounds:
        notes.append("the converge ledger has no recorded round yet")
    return _Part(rounds, settled, _ledger_summary(ledger, rounds, settled))


def _settled_view(entry: SettledEntry) -> SettledView:
    identity = entry.identity
    return SettledView(
        severity=identity.severity,
        file=identity.file,
        summary=identity.summary_normalized,
        outcome=entry.outcome,
        rationale=entry.rationale,
        round=entry.round,
        origin=entry.origin,
        actor=entry.actor,
        settled_at=entry.settled_at,
        reviewed_sha=entry.reviewed_sha,
    )


def _override_view(override: ConfirmOverride) -> OverrideView:
    return OverrideView(
        at=override.at,
        actor=override.actor,
        reason=override.reason,
        prior_status=override.prior_status,
        finding_count=len(override.identities),
    )


def _ledger_round_view(
    record: RoundRecord,
    settled: list[SettledView],
    split: SeveritySplit | None,
    override: OverrideView | None = None,
) -> RoundView:
    return RoundView(
        round=record.round,
        kind=record.kind,
        source="ledger",
        backend=record.backend,
        reviewed_sha=record.reviewed_sha,
        roster=list(record.roster),
        new_accepted=record.new_accepted,
        new_fix=record.new_fix,
        new_operator_action=record.new_operator_action,
        new_settled=record.new_settled,
        resurfaced_settled=record.resurfaced_settled,
        settled_total=record.new_settled + record.resurfaced_settled,
        new_by_severity=split,
        fix_dispatched=record.new_fix,
        clean=record.clean,
        recorded_at=record.recorded_at,
        settled=settled,
        override=override,
    )


def _severity_split(
    ledger: ConvergenceLedger,
    record: RoundRecord,
    verdict: ReviewVerdict | None,
    notes: list[str],
) -> SeveritySplit | None:
    """MUST_FIX/SHOULD_FIX split of round *record*'s new findings, if it checks out.

    Runs the engine's own new-accepted rule (``classify_findings``) over the
    round's verdict file against the ledger as it stood before this round
    (settled entries of earlier rounds only), and returns the split only when
    its total equals the ledger's own ``new_fix + new_operator_action``.
    """
    if verdict is None:
        return None
    try:
        findings, _ = from_verdict(verdict)
    except ConvergenceError:
        notes.append(
            f"round {record.round}: verdict file holds an undecided finding; "
            "severity split not shown"
        )
        return None
    before = ledger.model_copy(
        update={"settled": [e for e in ledger.settled if e.round < record.round]}
    )
    classified = classify_findings(before, findings, record.round)
    new = [*classified.new_fix, *classified.new_operator]
    expected = record.new_fix + record.new_operator_action
    if len(new) != expected:
        notes.append(
            f"round {record.round}: verdict file has {len(new)} new MUST_FIX/"
            f"SHOULD_FIX finding(s) but the ledger recorded {expected}; "
            "severity split not shown"
        )
        return None
    must = sum(1 for f in new if f.severity == "MUST_FIX")
    return SeveritySplit(must_fix=must, should_fix=len(new) - must)


def _ledger_summary(
    ledger: ConvergenceLedger, rounds: list[RoundView], settled: list[SettledView]
) -> HistorySummary:
    converged = ledger.status == LEDGER_STATUS_CONVERGED
    deferred = [s for s in settled if s.outcome == "defer"]
    return HistorySummary(
        convergence_mode="clean",
        rounds_run=len(ledger.rounds),
        converged=converged,
        final_clean_sha=ledger.last_clean_sha if converged else None,
        ledger_status=ledger.status,
        stop_reason=ledger.stop_reason,
        stop_identities=[i.audit_key() for i in ledger.stop_identities],
        fix_cycles_used=ledger.fix_cycles_used,
        max_cycles=ledger.max_cycles,
        confirm_backend=ledger.confirm_backend,
        new_accepted_series=_series(rounds),
        deferred=deferred,
        deferred_count=len(deferred),
        releases=[
            ReleaseView(
                at=r.at, round=r.round, reason=r.reason, cause=r.cause, sha=r.sha
            )
            for r in ledger.releases
        ],
        confirm_failures=len(ledger.confirm_failures),
    )
