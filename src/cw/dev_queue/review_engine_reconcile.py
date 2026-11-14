"""Reconcile a FAIL engine summary whose gating findings were deferred (GitHub #3625).

A repo review engine round can FAIL on SHOULD_FIX findings that the same round
then deferred (the adjudicator or the operator settled them ``defer``). Without
this module the review attestation skips such a head with ``engine_mismatch``
and the row stays unattested. :func:`reconcile_deferred_summary` turns exactly
that case into a PASS: the deferred findings become DEBT, and everything
downstream reads the result unchanged.

Authority is the row, not the worktree. The row's
``review_engine_settlements`` record for the head is what authorizes a
fingerprint, and the worktree ledger's settled ``defer`` entry only corroborates
it. The ledger can narrow the decision, never widen it, and a forged ledger alone
grants nothing. This module is pure: no I/O, no git, no lock, no subprocess
(ADR-0019), and it imports no ``crud``, so it cannot close an import cycle.

The reconciler refuses (returns None) unless every condition holds. It never
reconciles a MUST_FIX, a ``coverage.*`` or an ``acceptance.*`` finding, and it
does nothing when the current summary carries one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.models.review_engine_summary import EngineCounts, ReviewEngineSummary
from cw.review_convergence import settled_applies
from cw.review_engine.route import is_non_deferrable_rule

if TYPE_CHECKING:
    from collections.abc import Collection

    from cw.models import TicketTask
    from cw.models.review_engine_summary import EngineFinding
    from cw.review_convergence import ConvergenceLedger

_GATING: Final = frozenset({"MUST_FIX", "SHOULD_FIX"})
_RECONCILED_SEVERITY: Final = "SHOULD_FIX"
_DEBT_SEVERITY: Final = "DEBT"


def engine_settled_fingerprints(task: TicketTask, head_sha: str) -> frozenset[str]:
    """The fingerprints the row's settlement records list for *head_sha*. Pure read."""
    fingerprints: set[str] = set()
    for record in task.review_engine_settlements:
        if record.head_sha == head_sha:
            fingerprints.update(record.fingerprints)
    return frozenset(fingerprints)


def _ledger_defers(finding: EngineFinding, ledger: ConvergenceLedger) -> bool:
    """Whether the ledger holds a ``defer`` that silences *finding* as a SHOULD_FIX."""
    return any(
        entry.identity.fingerprint == finding.fingerprint
        and settled_applies(
            entry, _RECONCILED_SEVERITY, finding.fingerprint, finding.rule_id
        )
        for entry in ledger.settled
    )


def _reconcilable(
    finding: EngineFinding,
    ledger: ConvergenceLedger,
    settled_fingerprints: Collection[str],
) -> bool:
    return (
        finding.severity == _RECONCILED_SEVERITY
        and not is_non_deferrable_rule(finding.rule_id)
        and finding.fingerprint in settled_fingerprints
        and _ledger_defers(finding, ledger)
    )


def reconcile_deferred_summary(
    summary: ReviewEngineSummary,
    *,
    ledger: ConvergenceLedger,
    settled_fingerprints: Collection[str],
) -> ReviewEngineSummary | None:
    """A PASS copy of *summary* with its deferred gating findings as DEBT, or None.

    None unless the summary is a COMPLETE FAIL with at least one gating finding,
    and every gating finding is a SHOULD_FIX that is not a coverage or
    acceptance rule, whose fingerprint is in *settled_fingerprints* (the row's
    record for this head) and that the ledger settles ``defer``. The input is
    never mutated.
    """
    if summary.verdict != "FAIL" or summary.coverage != "COMPLETE":
        return None
    gating = [finding for finding in summary.findings if finding.severity in _GATING]
    if not gating:
        return None
    if not all(
        _reconcilable(finding, ledger, settled_fingerprints) for finding in gating
    ):
        return None
    remapped = [
        finding.model_copy(update={"severity": _DEBT_SEVERITY})
        if finding.severity in _GATING
        else finding
        for finding in summary.findings
    ]
    return summary.model_copy(
        update={
            "verdict": "PASS",
            "findings": remapped,
            "counts": EngineCounts.tally(remapped),
        }
    )
