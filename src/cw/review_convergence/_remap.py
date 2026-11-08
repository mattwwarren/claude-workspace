"""The proof-carrying move of ``last_clean_sha`` across a content-equal rebase (#2921).

A ``repair_changelog_conflict`` repair rebases every branch commit onto an
advanced main, so the ledger's ``last_clean_sha`` (the reviewed head) is never
the new head and the strict tip rule reports ``commits_after_clean_round``.
The recipe proves each branch commit unchanged (``git range-diff``) and that
the only delta is the rebuilt CHANGELOG commit, then calls
:func:`remap_clean_sha` to move the pointer to the new head with an audit
entry. The tip rule itself is untouched: after the move ``evaluate_tip``
accepts the new head by its ordinary strict equality.

Pure: returns an updated copy, reads no clock and writes nothing. The caller
persists the result only after the full fence passes.
"""

from __future__ import annotations

from cw.review_convergence._models import (
    LEDGER_STATUS_CONVERGED,
    CleanShaRemap,
    ConvergenceLedger,
)


def remap_clean_sha(
    ledger: ConvergenceLedger, remap: CleanShaRemap
) -> ConvergenceLedger | str:
    """Move *ledger*'s ``last_clean_sha`` to ``remap.to_sha``, or say why not.

    Refused (a reason string, the input untouched) unless the ledger is
    converged with no open confirm, its ``last_clean_sha`` is exactly
    ``remap.from_sha`` (a ledger tolerated past the clean sha is never
    remapped), and the move is a real one. ``rounds[]``, ``status``,
    ``checkpoint_3a_sha`` and ``fork_point`` are kept as they were.
    """
    if ledger.status != LEDGER_STATUS_CONVERGED:
        return f"ledger status is {ledger.status!r}, not converged"
    if ledger.awaiting_confirm_sha is not None:
        return "ledger has an unconfirmed round open (awaiting_confirm_sha)"
    if ledger.last_clean_sha is None:
        return "ledger has no last_clean_sha to remap"
    if ledger.last_clean_sha != remap.from_sha:
        return (
            f"last_clean_sha {ledger.last_clean_sha} is not the remap source"
            f" {remap.from_sha}"
        )
    if remap.from_sha == remap.to_sha:
        return "remap source and target are the same sha"
    return ledger.model_copy(
        update={
            "last_clean_sha": remap.to_sha,
            "clean_sha_remaps": [*ledger.clean_sha_remaps, remap],
        }
    )
