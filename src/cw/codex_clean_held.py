"""The park blocker for an evidence gap carried across a clean loop round (#3823).

A codex clean loop round can stop the ledger on a gap an earlier round held:
the gap is operator-actionable, so no repair worker is routed for it. The
blocker is built from the ledger's held memory, which neither the fix-loop
``_held_exit`` (it needs a verdict) nor the verdict synthesis (it needs a
verdict) can serve. It lives beside ``codex_clean_confirm`` (953 lines) and
imports nothing from it, so there is no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import Blocker
from cw.codex_review import STAGE3_REVIEW
from cw.review_convergence import outstanding_held, stop_summary

if TYPE_CHECKING:
    from cw.review_convergence import ConvergenceLedger

#: The blocker reason an evidence gap parks with (the same reason the fix loop uses).
HELD_BLOCKER_REASON = "review_operator_actionable"

#: The first line of the details: the operator, not a worker, must act.
HELD_HEADLINE = (
    "OPERATOR EVIDENCE REQUIRED: a reviewer could not run a check; "
    "no repair worker is routed for it."
)


def _held_line(entry_file: str, summary: str, severity: str, rationale: str) -> str:
    """One outstanding gap: its location, its summary and the unrun check."""
    return f"- {severity} {entry_file}: {summary} (unrun check: {rationale})"


def held_stop_blocker(ledger: ConvergenceLedger) -> Blocker:
    """The ``review_operator_actionable`` park for *ledger*'s outstanding gaps.

    Names each outstanding held entry with its unrun check, then the release
    hint ``stop_summary`` gives for the stopped ledger. Never retryable: a
    worker cannot run the check, only the operator can.
    """
    gap_lines = [
        _held_line(
            entry.identity.file,
            entry.identity.summary_normalized,
            entry.identity.severity,
            entry.rationale,
        )
        for entry in outstanding_held(ledger)
    ]
    release_hint = (stop_summary(ledger) or {}).get("release_hint", "")
    lines = [HELD_HEADLINE, *gap_lines, f"release: {release_hint}"]
    return Blocker(
        stage=STAGE3_REVIEW,
        reason=HELD_BLOCKER_REASON,
        details="\n".join(lines),
        retry_eligible=False,
    )
