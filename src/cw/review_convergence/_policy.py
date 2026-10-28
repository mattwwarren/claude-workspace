"""Pure predicates over the ledger that several engine modules share (#2787).

``max_cycles_exhausted`` and ``release_cause`` live here, below both
``record_round`` and ``operator_settle``/``reconcile_ledger_for_init``, so the
cap check ``record_round`` stops on and the one the release check consults are
the same function and cannot disagree. ``detect_no_progress`` and
``detect_oscillation`` are the two "not converging" detectors.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from cw.review_convergence._identity import finding_identity
from cw.review_convergence._models import (
    STALL_WINDOW,
    STOP_MAX_CYCLES,
    STOP_OPERATOR_ACTION,
    STOP_OSCILLATION,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence._identity import FindingIdentity
    from cw.review_convergence._models import (
        ConvergenceLedger,
        ReleaseCause,
        RoundFinding,
        RoundRecord,
    )


def max_cycles_exhausted(ledger: ConvergenceLedger) -> bool:
    """True when the epoch has dispatched as many fix cycles as the cap allows."""
    return ledger.fix_cycles_used >= ledger.max_cycles


def detect_no_progress(
    rounds: Sequence[RoundRecord], *, window: int = STALL_WINDOW, start: int = 0
) -> bool:
    """True when the last ``window + 1`` rounds from *start* are a stalled series.

    A stalled series is all non-clean with a non-decreasing ``new_accepted``
    count for ``window`` consecutive comparisons (3,3,3 and 2,3,4 stall; 5,2,1
    is progress; a clean round in the window, such as the 0 of a clean loop
    round whose confirm round then found something, breaks the series).
    """
    considered = rounds[start:]
    if len(considered) < window + 1:
        return False
    tail = considered[-(window + 1) :]
    if any(record.clean for record in tail):
        return False
    counts = [record.new_accepted for record in tail]
    return all(later >= earlier for earlier, later in itertools.pairwise(counts))


def detect_oscillation(
    ledger: ConvergenceLedger,
    new_accepted: Sequence[RoundFinding],
    reviewed_sha: str,
) -> list[FindingIdentity]:
    """Identities of *new_accepted* findings that were marked fixed and are back.

    Considers the whole fixed memory (across epochs, because a recurrence is
    real wherever it happens) but counts an identity only when its
    ``FixedEntry.reviewed_sha`` differs from *reviewed_sha*: a recurrence needs
    a fix commit in between, so a same-tip re-review after a stop release does
    not trip on its own history. Order follows *new_accepted*, deduplicated.
    """
    fixed_at = {entry.identity: entry.reviewed_sha for entry in ledger.fixed}
    recurring: list[FindingIdentity] = []
    for finding in new_accepted:
        identity = finding_identity(finding.severity, finding.file, finding.summary)
        fixed_sha = fixed_at.get(identity)
        if fixed_sha is None or fixed_sha == reviewed_sha:
            continue
        if identity not in recurring:
            recurring.append(identity)
    return recurring


def release_cause(ledger: ConvergenceLedger) -> ReleaseCause | None:
    """Why a stopped ledger may be released WITHOUT a new commit, else ``None``.

    The same-tip release rule (R11 to R13)::

        max_cycles       and the refreshed cap is not exhausted -> max_cycles_raised
        operator_action                                         -> operator_action_done
        oscillation      and every identity operator-settled    -> identities_settled
        anything else (no_progress, an unsettled oscillation,
        an unrecognised reason)                                 -> None (terminal)
    """
    reason = ledger.stop_reason
    if reason == STOP_MAX_CYCLES and not max_cycles_exhausted(ledger):
        return "max_cycles_raised"
    if reason == STOP_OPERATOR_ACTION:
        return "operator_action_done"
    if reason == STOP_OSCILLATION and _all_operator_settled(ledger):
        return "identities_settled"
    return None


def _all_operator_settled(ledger: ConvergenceLedger) -> bool:
    """True when every recorded stop identity has an operator-origin settle."""
    if not ledger.stop_identities:
        return False
    operator_settled = {
        entry.identity for entry in ledger.settled if entry.origin == "operator"
    }
    return all(identity in operator_settled for identity in ledger.stop_identities)


def consecutive_confirm_failures(ledger: ConvergenceLedger, *, cause: str) -> int:
    """The trailing run of *cause* confirm failures at the current candidate (R26).

    Derived, never stored: walks ``confirm_failures`` from the end counting
    entries whose cause is *cause*, whose ``candidate_sha`` is the ledger's
    current ``awaiting_confirm_sha`` and whose ``round`` is ``len(rounds)``,
    stopping at the first entry that differs. A different cause, a new
    candidate sha, or any recorded round (which moves ``len(rounds)``) ends the
    run; a requeue, a stop release and the passage of time do not.
    """
    count = 0
    for failure in reversed(ledger.confirm_failures):
        if (
            failure.cause != cause
            or failure.candidate_sha != ledger.awaiting_confirm_sha
            or failure.round != len(ledger.rounds)
        ):
            break
        count += 1
    return count
