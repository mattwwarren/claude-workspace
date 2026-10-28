"""The converge-to-clean state machine: recording rounds and settling (#2787).

Every function here is pure: it takes a ledger, returns an updated COPY, and
reads no clock (callers pass ``now`` as an ISO-8601 UTC string). The CLI saves
the ledger only after the engine returns, so a function that raises leaves
nothing behind.

Decision priority in :func:`record_round`: clean (behind the confirm gate), then
oscillation, then an ``operator_action`` finding, then no-progress, then the
``max_cycles`` backstop, otherwise continue. ``operator_action`` outranks
no-progress because its stop is operator-releasable while no-progress is
terminal, so a mixed round reports the stop the operator can act on.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.review_convergence._findings import classify_findings
from cw.review_convergence._identity import FindingIdentity, finding_identity
from cw.review_convergence._models import (
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    LEDGER_STATUS_STOPPED,
    STOP_MAX_CYCLES,
    STOP_NO_PROGRESS,
    STOP_OPERATOR_ACTION,
    STOP_OSCILLATION,
    ConfirmFailure,
    ConvergenceError,
    ConvergenceLedger,
    Decision,
    FixedEntry,
    OperatorSettleReport,
    RoundRecord,
    RoundReport,
    SettledEntry,
    UnmatchedTarget,
)
from cw.review_convergence._policy import (
    consecutive_confirm_failures,
    detect_no_progress,
    detect_oscillation,
    max_cycles_exhausted,
    release_cause,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence._backend import ResolvedBackend
    from cw.review_convergence._findings import Classified
    from cw.review_convergence._models import (
        FrozenCounts,
        RoundFinding,
        RoundKind,
        SettleTarget,
        UnreadFindings,
    )

#: A confirm backend may park ``confirm_backend_unavailable`` (uncharged) this
#: many times in a row for one cause at one sha; the next one escalates (R26).
CONFIRM_UNAVAILABLE_CAP = 3

#: ``difflib`` similarity floor for naming the nearest recorded identity.
_NEAREST_CUTOFF = 0.6


@dataclass(frozen=True)
class _Outcome:
    """What a round decided: the decision, a stop reason and its identities."""

    decision: Decision
    reason: str | None = None
    stop_identities: list[FindingIdentity] = field(default_factory=list)


def new_ledger(
    *,
    ticket_id: str,
    fork_point: str,
    checkpoint_3a_sha: str,
    max_cycles: int,
    loop_backend: str,
    confirm: ResolvedBackend,
) -> ConvergenceLedger:
    """A fresh, empty ledger for ``(ticket_id, fork_point)``."""
    return ConvergenceLedger(
        ticket_id=ticket_id,
        fork_point=fork_point,
        max_cycles=max_cycles,
        loop_backend=loop_backend,
        confirm_backend=confirm.name,
        confirm_not_independent=confirm.not_independent,
        checkpoint_3a_sha=checkpoint_3a_sha,
    )


def _require_open_ledger(ledger: ConvergenceLedger) -> None:
    if ledger.status in {LEDGER_STATUS_STOPPED, LEDGER_STATUS_CONVERGED}:
        msg = (
            f"cannot record a round on a {ledger.status} ledger; run "
            "'converge init' to reconcile it first"
        )
        raise ConvergenceError(msg)


def _require_confirm_preconditions(
    ledger: ConvergenceLedger, backend: str, reviewed_sha: str
) -> None:
    if ledger.status != LEDGER_STATUS_AWAITING_CONFIRM:
        msg = (
            "a confirm round requires an awaiting_confirm ledger "
            f"(status is {ledger.status})"
        )
        raise ConvergenceError(msg)
    if reviewed_sha != ledger.awaiting_confirm_sha:
        msg = (
            f"confirm round sha {reviewed_sha} is not the awaiting-confirm "
            f"candidate {ledger.awaiting_confirm_sha}"
        )
        raise ConvergenceError(msg)
    if backend != ledger.confirm_backend:
        msg = (
            f"confirm round backend {backend!r} is not the ledger's confirm "
            f"backend {ledger.confirm_backend!r}"
        )
        raise ConvergenceError(msg)


def _guard_unread(unread: UnreadFindings | None) -> None:
    """Refuse to record as clean a round that lost findings or reviewers (R29)."""
    if unread is None:
        return
    if unread.rejected_must_fix or unread.discarded_run_failures:
        msg = (
            f"unread_findings: {unread.rejected_must_fix} MUST_FIX finding(s) "
            "rejected by the consolidator and "
            f"{unread.discarded_run_failures} reviewer failure(s) that discarded "
            "MUST_FIX or SHOULD_FIX findings; a round with findings nobody read "
            "is not a clean round"
        )
        raise ConvergenceError(msg)
    if unread.failed_roles:
        msg = (
            f"failed_role: {len(unread.failed_roles)} reviewer role(s) failed "
            f"outright ({', '.join(unread.failed_roles)}); a round with a role "
            "that never reviewed is not a clean round"
        )
        raise ConvergenceError(msg)


def _clean_decision(ledger: ConvergenceLedger, kind: RoundKind) -> Decision:
    """A clean loop round needs an independent confirm round; anything else is done."""
    needs_confirm = (
        kind == "loop"
        and ledger.confirm_backend != "none"
        and not ledger.confirm_not_independent
    )
    return "confirm_required" if needs_confirm else "clean"


def _decide(
    ledger: ConvergenceLedger,
    classified: Classified,
    record: RoundRecord,
    rounds: Sequence[RoundRecord],
) -> _Outcome:
    if record.clean:
        return _Outcome(_clean_decision(ledger, record.kind))
    accepted = [*classified.new_fix, *classified.new_operator]
    recurring = detect_oscillation(ledger, accepted, record.reviewed_sha)
    if recurring:
        return _Outcome("stop_not_converging", STOP_OSCILLATION, recurring)
    if classified.new_operator:
        identities: list[FindingIdentity] = []
        for finding in classified.new_operator:
            identity = finding_identity(finding.severity, finding.file, finding.summary)
            if identity not in identities:
                identities.append(identity)
        return _Outcome("stop_operator_action", STOP_OPERATOR_ACTION, identities)
    start = max(ledger.epoch_start_round, ledger.window_start_round)
    if detect_no_progress(rounds, window=ledger.stall_window, start=start):
        return _Outcome("stop_not_converging", STOP_NO_PROGRESS)
    if max_cycles_exhausted(ledger):
        return _Outcome("stop_max_cycles", STOP_MAX_CYCLES)
    return _Outcome("continue")


def _fixed_after_continue(
    ledger: ConvergenceLedger, classified: Classified, record: RoundRecord
) -> list[FixedEntry]:
    """Fixed memory plus the identities a dispatched fix cycle is about to address."""
    known = {entry.identity for entry in ledger.fixed}
    fixed = list(ledger.fixed)
    for finding in classified.new_fix:
        identity = finding_identity(finding.severity, finding.file, finding.summary)
        if identity not in known:
            known.add(identity)
            fixed.append(
                FixedEntry(
                    identity=identity,
                    round=record.round,
                    reviewed_sha=record.reviewed_sha,
                )
            )
    return fixed


def _transition(
    ledger: ConvergenceLedger,
    outcome: _Outcome,
    classified: Classified,
    record: RoundRecord,
) -> dict[str, object]:
    """The ledger fields a decision changes."""
    decision = outcome.decision
    if decision == "clean":
        return {
            "status": LEDGER_STATUS_CONVERGED,
            "last_clean_sha": record.reviewed_sha,
        }
    if decision == "confirm_required":
        return {
            "status": LEDGER_STATUS_AWAITING_CONFIRM,
            "awaiting_confirm_sha": record.reviewed_sha,
        }
    if decision == "continue":
        return {
            "status": LEDGER_STATUS_IN_PROGRESS,
            "fix_cycles_used": ledger.fix_cycles_used + 1,
            "fixed": _fixed_after_continue(ledger, classified, record),
        }
    return {
        "status": LEDGER_STATUS_STOPPED,
        "stop_reason": outcome.reason,
        "stop_identities": outcome.stop_identities,
    }


def record_round(
    ledger: ConvergenceLedger,
    *,
    kind: RoundKind,
    backend: str,
    reviewed_sha: str,
    findings: list[RoundFinding],
    roster: Sequence[str],
    now: str,
    unread: UnreadFindings | None = None,
    frozen: FrozenCounts | None = None,
) -> tuple[ConvergenceLedger, RoundReport]:
    """Record one review round and decide what happens next.

    Returns the updated ledger and a :class:`RoundReport`. Raises
    :class:`ConvergenceError`, before any state change, for: a ledger that is
    stopped or converged; a ``confirm`` round that is not at the
    ``awaiting_confirm`` ledger's sha and confirm backend; and a would-be-clean
    round (zero new accepted findings) while *unread* shows findings a
    reviewer lost (``unread_findings:``, R29) or a role that failed outright
    (``failed_role:``). A dirty round with unread findings proceeds, because the
    next full round re-reviews the diff. The guard raises rather than returning
    a decision, so no literal exists that a caller could ignore.

    *frozen* is stored the first time it is given (the cycle-0 baseline).
    """
    _require_open_ledger(ledger)
    if kind == "confirm":
        _require_confirm_preconditions(ledger, backend, reviewed_sha)
    round_number = len(ledger.rounds) + 1
    classified = classify_findings(ledger, findings, round_number)
    if classified.new_accepted == 0:
        _guard_unread(unread)
    record = RoundRecord(
        round=round_number,
        kind=kind,
        backend=backend,
        reviewed_sha=reviewed_sha,
        new_accepted=classified.new_accepted,
        new_fix=len(classified.new_fix),
        new_operator_action=len(classified.new_operator),
        new_settled=len(classified.newly_settled),
        resurfaced_settled=classified.resurfaced,
        clean=classified.new_accepted == 0,
        roster=list(roster),
        recorded_at=now,
    )
    rounds = [*ledger.rounds, record]
    outcome = _decide(ledger, classified, record, rounds)
    update: dict[str, object] = {
        "rounds": rounds,
        "settled": [*ledger.settled, *classified.newly_settled],
        "awaiting_confirm_sha": None,
    }
    if ledger.frozen is None and frozen is not None:
        update["frozen"] = frozen
    update.update(_transition(ledger, outcome, classified, record))
    updated = ledger.model_copy(update=update)
    return updated, _report(updated, outcome, record)


def _report(
    ledger: ConvergenceLedger, outcome: _Outcome, record: RoundRecord
) -> RoundReport:
    dispatched = outcome.decision == "continue"
    return RoundReport(
        decision=outcome.decision,
        reason=outcome.reason,
        round=record.round,
        new_accepted=record.new_accepted,
        new_settled=record.new_settled,
        resurfaced_settled=record.resurfaced_settled,
        fix_cycles_used=ledger.fix_cycles_used,
        next_fix_cycle=ledger.fix_cycles_used + (0 if dispatched else 1),
        history=[r.new_accepted for r in ledger.rounds[ledger.epoch_start_round :]],
        confirm_not_independent=ledger.confirm_not_independent,
    )


def record_confirm_failure(
    ledger: ConvergenceLedger, *, cause: str, detail: str, now: str
) -> tuple[ConvergenceLedger, RoundReport]:
    """Record a confirm-backend outage against the awaiting-confirm candidate.

    Adds no round and changes no counter or status: the ledger stays
    ``awaiting_confirm`` at the same sha. The decision is
    ``stop_confirm_unavailable`` (an uncharged park) unless the trailing run of
    this *cause* at this sha, counted before the new entry, already reached
    :data:`CONFIRM_UNAVAILABLE_CAP`, in which case it is
    ``stop_confirm_escalated`` and stays so until the run resets (R26).
    """
    candidate = ledger.awaiting_confirm_sha
    if ledger.status != LEDGER_STATUS_AWAITING_CONFIRM or candidate is None:
        msg = (
            "a confirm failure requires an awaiting_confirm ledger "
            f"(status is {ledger.status})"
        )
        raise ConvergenceError(msg)
    prior = consecutive_confirm_failures(ledger, cause=cause)
    entry = ConfirmFailure(
        at=now,
        cause=cause,
        detail=detail,
        backend=ledger.confirm_backend,
        candidate_sha=candidate,
        round=len(ledger.rounds),
    )
    updated = ledger.model_copy(
        update={"confirm_failures": [*ledger.confirm_failures, entry]}
    )
    decision: Decision = (
        "stop_confirm_escalated"
        if prior >= CONFIRM_UNAVAILABLE_CAP
        else "stop_confirm_unavailable"
    )
    report = RoundReport(
        decision=decision,
        reason=cause,
        round=len(updated.rounds),
        new_accepted=0,
        fix_cycles_used=updated.fix_cycles_used,
        next_fix_cycle=updated.fix_cycles_used + 1,
        history=[r.new_accepted for r in updated.rounds[updated.epoch_start_round :]],
        confirm_not_independent=updated.confirm_not_independent,
    )
    return updated, report


def _recorded_identities(ledger: ConvergenceLedger) -> dict[str, FindingIdentity]:
    """Every identity the ledger has seen, keyed by audit key, in a stable order."""
    seen: dict[str, FindingIdentity] = {}
    sources = (
        ledger.stop_identities,
        [entry.identity for entry in ledger.fixed],
        [entry.identity for entry in ledger.settled],
    )
    for identities in sources:
        for identity in identities:
            seen.setdefault(identity.audit_key(), identity)
    return seen


def _nearest(
    identity: FindingIdentity, recorded: dict[str, FindingIdentity]
) -> FindingIdentity | None:
    close = difflib.get_close_matches(
        identity.audit_key(), list(recorded), n=1, cutoff=_NEAREST_CUTOFF
    )
    return recorded[close[0]] if close else None


def operator_settle(
    ledger: ConvergenceLedger,
    targets: Sequence[SettleTarget],
    *,
    actor: str,
    reason: str,
    now: str,
) -> tuple[ConvergenceLedger, OperatorSettleReport]:
    """Settle recurring findings an operator judges false positives (R12).

    A target matches when its identity appears in ``stop_identities``, ``fixed``
    or ``settled``; each match adds (or refreshes, newest wins, one entry per
    identity) an operator-origin ``reject`` :class:`SettledEntry` carrying the
    audit trail. Unmatched targets are reported with their nearest recorded
    identity and never recorded: the engine has no path that settles an
    identity the ledger has not already seen. When nothing matches, the ledger
    is returned unchanged. ``releasable`` is whether a same-tip requeue would
    now release the stop. Raises :class:`ConvergenceError` for a ledger with no
    rounds.
    """
    if not ledger.rounds:
        msg = "cannot settle findings on a ledger with no recorded rounds"
        raise ConvergenceError(msg)
    recorded = _recorded_identities(ledger)
    reviewed_sha = ledger.rounds[-1].reviewed_sha
    matched: dict[FindingIdentity, SettledEntry] = {}
    unmatched: list[UnmatchedTarget] = []
    for target in targets:
        identity = finding_identity(target.severity, target.file, target.summary)
        if identity.audit_key() in recorded:
            matched[identity] = SettledEntry(
                identity=identity,
                outcome="reject",
                rationale=reason,
                round=len(ledger.rounds),
                origin="operator",
                actor=actor,
                settled_at=now,
                reviewed_sha=reviewed_sha,
            )
        else:
            unmatched.append(
                UnmatchedTarget(target=target, nearest=_nearest(identity, recorded))
            )
    if not matched:
        report = OperatorSettleReport(
            settled=[],
            unmatched=unmatched,
            releasable=release_cause(ledger) is not None,
        )
        return ledger, report
    pending = dict(matched)
    settled = [pending.pop(entry.identity, entry) for entry in ledger.settled]
    settled.extend(pending.values())
    updated = ledger.model_copy(update={"settled": settled})
    report = OperatorSettleReport(
        settled=list(matched.values()),
        unmatched=unmatched,
        releasable=release_cause(updated) is not None,
    )
    return updated, report
