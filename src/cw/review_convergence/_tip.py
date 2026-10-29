"""The strict final-sha acceptance rule for clean mode (#2787, R22).

:func:`evaluate_tip` is the single rule, defined once and called by both
``converge finalize`` (Stage 3 exit) and ``converge verify-tip`` (finalize
Step 4.0b). It is pure and takes two SHAs: the CLI reads them with git, after a
``git fetch origin <branch>`` so the origin tip is never a stale tracking ref.
It is strict unless the caller established that everything after the clean sha
is a merge of ``origin/<default>`` that left the branch's files untouched.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_convergence._models import (
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    TipVerdict,
)

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger


def finalize_ready(ledger: ConvergenceLedger) -> bool:
    """True when the ledger converged with a recorded clean sha.

    ``record_round`` only reaches ``converged`` through the confirm rule, so an
    unconfirmed loop round can never satisfy this.
    """
    return (
        ledger.status == LEDGER_STATUS_CONVERGED and ledger.last_clean_sha is not None
    )


def evaluate_tip(
    ledger: ConvergenceLedger,
    *,
    head_sha: str,
    origin_tip_sha: str,
    default_merge_only: bool = False,
) -> TipVerdict:
    """Accept the tip only if what is pushed is exactly what was last reviewed clean.

    ``accepted`` iff all of: the ledger is :func:`finalize_ready`; ``head_sha``
    equals ``origin_tip_sha`` (what is pushed is HEAD); and ``head_sha`` equals
    ``last_clean_sha`` (no commit of any kind landed after the last clean round),
    unless the caller passes ``default_merge_only=True``. The caller sets that
    only after establishing that everything after ``last_clean_sha`` is a merge
    of ``origin/<default>`` that left the branch's own files untouched; this
    function never inspects git. Otherwise ``rereview_required`` with the first
    failing condition as the reason, in that order: ``no_clean_round``,
    ``head_not_origin_tip``, ``commits_after_clean_round``.
    """
    if not finalize_ready(ledger):
        return TipVerdict(decision="rereview_required", reason="no_clean_round")
    if head_sha != origin_tip_sha:
        return TipVerdict(decision="rereview_required", reason="head_not_origin_tip")
    if head_sha != ledger.last_clean_sha and not default_merge_only:
        return TipVerdict(
            decision="rereview_required", reason="commits_after_clean_round"
        )
    return TipVerdict(decision="accepted")


def demote_for_rereview(ledger: ConvergenceLedger) -> ConvergenceLedger:
    """Reopen a converged ledger as ``in_progress`` so another full round runs.

    ``finalize`` calls this only for ``commits_after_clean_round`` (a
    review-state defect). Settled and fixed memory, the epoch and
    ``fix_cycles_used`` are kept; ``finalize_ready`` is false afterwards
    because it is derived from ``status``. An already open ledger is returned
    unchanged.
    """
    if ledger.status != LEDGER_STATUS_CONVERGED:
        return ledger
    return ledger.model_copy(
        update={
            "status": LEDGER_STATUS_IN_PROGRESS,
            "awaiting_confirm_sha": None,
        }
    )
