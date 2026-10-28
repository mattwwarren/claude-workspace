"""The ``converge status`` summary (#2787).

:func:`next_action` tells a resumed REVIEW session what to do next, from the
ledger and the worktree's HEAD alone; :func:`status_summary` wraps it with the
rest of what the session needs to reconstruct its state.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.review_convergence._models import (
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_STOPPED,
)
from cw.review_convergence._render import sentinel_review_fragment, stop_summary

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger

NextAction = Literal["loop_round", "confirm", "fix", "done", "stopped"]


def next_action(ledger: ConvergenceLedger, *, head_sha: str) -> NextAction:
    """What a session resumed at *head_sha* should do, by this precedence.

    1. ``done``: converged and HEAD is ``last_clean_sha`` (a converged ledger on
       a moved tip was reopened by ``init`` and falls through).
    2. ``stopped``: the ledger is stopped (terminal).
    3. ``confirm``: awaiting confirm and HEAD is ``awaiting_confirm_sha``.
    4. ``loop_round``: no round recorded since the latest release or reopening
       (``releases[-1].round == len(rounds)``), so the released tip is
       re-reviewed in full instead of replaying a dead session's verdict.
    5. ``fix``: the last round was dirty and HEAD is still the sha it reviewed
       (the fix cycle has not committed yet). Otherwise ``loop_round``: no
       round yet, or HEAD moved.
    """
    if ledger.status == LEDGER_STATUS_CONVERGED and head_sha == ledger.last_clean_sha:
        return "done"
    if ledger.status == LEDGER_STATUS_STOPPED:
        return "stopped"
    if (
        ledger.status == LEDGER_STATUS_AWAITING_CONFIRM
        and head_sha == ledger.awaiting_confirm_sha
    ):
        return "confirm"
    if ledger.releases and ledger.releases[-1].round == len(ledger.rounds):
        return "loop_round"
    if ledger.rounds:
        last = ledger.rounds[-1]
        if not last.clean and last.reviewed_sha == head_sha:
            return "fix"
    return "loop_round"


def status_summary(ledger: ConvergenceLedger, *, head_sha: str) -> dict[str, object]:
    """The ``converge status`` output.

    ``next_fix_cycle`` is the number of the fix cycle the session runs or would
    run next: ``fix_cycles_used`` while a dispatched cycle is pending
    (``next_action == "fix"``), otherwise ``fix_cycles_used + 1``. ``roster``
    is the latest recorded round's reviewer roster (empty before any round),
    so a resumed session re-spawns the same roles.
    """
    action = next_action(ledger, head_sha=head_sha)
    pending = action == "fix"
    return {
        "status": ledger.status,
        "next_action": action,
        "checkpoint_3a_sha": ledger.checkpoint_3a_sha,
        "frozen": ledger.frozen.model_dump(mode="json") if ledger.frozen else None,
        "fix_cycles_used": ledger.fix_cycles_used,
        "next_fix_cycle": ledger.fix_cycles_used + (0 if pending else 1),
        "max_cycles": ledger.max_cycles,
        "roster": list(ledger.rounds[-1].roster) if ledger.rounds else [],
        "awaiting_confirm_sha": ledger.awaiting_confirm_sha,
        "last_clean_sha": ledger.last_clean_sha,
        "stop": stop_summary(ledger),
        "releases": [r.model_dump(mode="json") for r in ledger.releases],
        "sentinel_review": sentinel_review_fragment(ledger),
    }
