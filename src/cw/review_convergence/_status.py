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
    DEFAULT_MERGE_TOLERATED_KEY,
    DELTA_CONFIRM_PENDING_KEY,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_STOPPED,
    OVERLAPPING_FILES_KEY,
)
from cw.review_convergence._render import sentinel_review_fragment, stop_summary

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger

NextAction = Literal["loop_round", "confirm", "fix", "done", "stopped", "delta_confirm"]


def _converged_action(
    ledger: ConvergenceLedger,
    *,
    head_sha: str,
    default_merge_tolerated: bool,
    delta_confirm_pending: bool,
) -> NextAction | None:
    """``done`` / ``delta_confirm`` for a converged ledger, else ``None``."""
    if ledger.status != LEDGER_STATUS_CONVERGED:
        return None
    if head_sha == ledger.last_clean_sha or default_merge_tolerated:
        return "done"
    return "delta_confirm" if delta_confirm_pending else None


def next_action(
    ledger: ConvergenceLedger,
    *,
    head_sha: str,
    default_merge_tolerated: bool = False,
    delta_confirm_pending: bool = False,
) -> NextAction:
    """What a session resumed at *head_sha* should do, by this precedence.

    1. ``done``: converged and HEAD is ``last_clean_sha``, or HEAD moved past
       it by tolerated default-branch merges only (*default_merge_tolerated*,
       #2861). ``delta_confirm``: converged, HEAD moved past it by a
       default-branch merge a delta confirm round may record
       (*delta_confirm_pending*, #2918), and the move is not tolerated: run
       ``cw review converge delta-confirm``. A converged ledger on any other
       moved tip was reopened by ``init`` and falls through.
    2. ``stopped``: the ledger is stopped (terminal).
    3. ``confirm``: awaiting confirm and HEAD is ``awaiting_confirm_sha``.
    4. ``loop_round``: no round recorded since the latest release or reopening
       (``releases[-1].round == len(rounds)``), so the released tip is
       re-reviewed in full instead of replaying a dead session's verdict.
    5. ``fix``: the last round was dirty and HEAD is still the sha it reviewed
       (the fix cycle has not committed yet). Otherwise ``loop_round``: no
       round yet, or HEAD moved.
    """
    converged = _converged_action(
        ledger,
        head_sha=head_sha,
        default_merge_tolerated=default_merge_tolerated,
        delta_confirm_pending=delta_confirm_pending,
    )
    if converged is not None:
        return converged
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


def status_summary(
    ledger: ConvergenceLedger,
    *,
    head_sha: str,
    default_merge_tolerated: bool = False,
    delta_confirm_pending: bool = False,
    overlapping_files: tuple[str, ...] = (),
) -> dict[str, object]:
    """The ``converge status`` output.

    ``next_fix_cycle`` is the number of the fix cycle the session runs or would
    run next: ``fix_cycles_used`` while a dispatched cycle is pending
    (``next_action == "fix"``), otherwise ``fix_cycles_used + 1``. ``roster``
    is the latest recorded round's reviewer roster (empty before any round),
    so a resumed session re-spawns the same roles. A
    ``default_merge_tolerated: true`` key is added only when HEAD is past the
    last clean round by tolerated merges only (#2861), so the output is
    otherwise unchanged. ``delta_confirm_pending: true`` and
    ``overlapping_files`` (*overlapping_files*, sorted) are added only when
    ``next_action`` is ``delta_confirm`` (#2918).
    """
    action = next_action(
        ledger,
        head_sha=head_sha,
        default_merge_tolerated=default_merge_tolerated,
        delta_confirm_pending=delta_confirm_pending,
    )
    pending = action == "fix"
    summary: dict[str, object] = {
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
    if default_merge_tolerated:
        summary[DEFAULT_MERGE_TOLERATED_KEY] = True
    if action == "delta_confirm":
        summary[DELTA_CONFIRM_PENDING_KEY] = True
        summary[OVERLAPPING_FILES_KEY] = list(overlapping_files)
    return summary
