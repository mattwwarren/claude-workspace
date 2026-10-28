"""Clean-mode convergence exemption for the queue-peek ladder (#2787, R5).

A clean-mode review loops until a full round comes back clean, so a healthy
REVIEW session legitimately runs past the ladder's age thresholds. The age
arms of :func:`cw.queue_peek.recommend` would flag it PEEK or STOP while the
new-finding count is still falling. This module reads the convergence ledger
the session persists (``<worktree>/.cw/review-convergence.json``) and, for
age-based verdicts on a demonstrably live REVIEW session, replaces them with
the convergence signal: WAIT while the curve falls, PEEK when it has stalled.

It lives beside the ``cw.queue_peek`` package rather than inside it and imports
nothing from it, so the hook in ``cw.queue_peek.rows.format_row`` creates no
import cycle. Every failure to read the
ledger fails open: no override, the bare ladder verdict stands.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from cw._hook_context import _read_cw_context
from cw.models import Stage
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_IN_PROGRESS,
    ConvergenceLedger,
    detect_no_progress,
    load_ledger,
)
from cw.spawn import CW_CONTEXT_SCHEMA_VERSION

if TYPE_CHECKING:
    from cw.models import TicketTask

#: Every age-arm reason in ``queue_peek.ladder._score_session`` /
#: ``_stall_check`` starts with this (``age 70min exceeded ...``), and
#: ``_gate_stop_on_liveness`` only appends to it.
#: ``tests/test_queue_peek_convergence.py`` pins it against the real
#: ``recommend()`` output, so a reworded ladder string fails a test.
AGE_REASON_PREFIX = "age "

#: Ledger statuses of a review that is still running. ``converged`` is waiting
#: for finalize and ``stopped`` is parked, so neither is exempted.
_IN_FLIGHT_STATUSES = frozenset(
    {LEDGER_STATUS_IN_PROGRESS, LEDGER_STATUS_AWAITING_CONFIRM}
)

#: A curve needs at least this many rounds to say anything about direction.
_MIN_CURVE_POINTS = 2


def is_age_based_reason(reason: str) -> bool:
    """True when *reason* came from one of the ladder's age arms."""
    return reason.startswith(AGE_REASON_PREFIX)


def _read_ledger(task: TicketTask) -> ConvergenceLedger | None:
    """The task's in-flight ledger, or ``None`` (absent, corrupt, unreadable, done)."""
    if task.worktree_path is None:
        return None
    if not _clean_mode_context(task):
        return None
    try:
        ledger = load_ledger(task.worktree_path / LEDGER_RELATIVE_PATH)
    except (OSError, ValueError):
        return None
    if ledger is None or ledger.status not in _IN_FLIGHT_STATUSES:
        return None
    if ledger.ticket_id != task.ticket_id:
        return None
    return ledger


def _clean_mode_context(task: TicketTask) -> bool:
    """Require the current worktree context to opt into clean mode."""
    if task.worktree_path is None:
        return False
    try:
        context = _read_cw_context(str(task.worktree_path))
    except UnicodeError:
        return False
    if context is None or context.get("schema_version") != CW_CONTEXT_SCHEMA_VERSION:
        return False
    review = context.get("review") if isinstance(context, dict) else None
    return isinstance(review, dict) and review.get("convergence") == "clean"


def _is_falling(counts: list[int]) -> bool:
    """True for a non-increasing series of at least two points that ends lower."""
    if len(counts) < _MIN_CURVE_POINTS:
        return False
    return counts[-1] < counts[0] and all(
        later <= earlier for earlier, later in itertools.pairwise(counts)
    )


def convergence_peek_override(
    task: TicketTask,
    rec: str,
    reason: str,
    *,
    idle_min: float | None,
    idle_live_max_min: float,
) -> tuple[str, str] | None:
    """Replace an age-based verdict on a live clean-mode review, else ``None``.

    Applies only to a REVIEW-stage row whose ledger is in flight, to an
    age-based *reason* (:func:`is_age_based_reason`), and only while
    ``idle_min <= idle_live_max_min``: the age STOP arm precedes the idle arms
    in the ladder, so a long review that is genuinely stalled (idle unknown or
    past the threshold) must still be flagged. A stalled ledger
    (``detect_no_progress``) yields PEEK, a falling new-finding curve WAIT; a
    flat or one-round curve yields no override. A *rec* that is already WAIT
    is left alone (there is nothing to downgrade) unless the ledger stalled.
    """
    if task.stage is not Stage.REVIEW or not is_age_based_reason(reason):
        return None
    if idle_min is None or idle_min > idle_live_max_min:
        return None
    ledger = _read_ledger(task)
    if ledger is None:
        return None
    rounds = ledger.rounds
    counts = [record.new_accepted for record in rounds[ledger.epoch_start_round :]]
    curve = "→".join(str(count) for count in counts)
    start = max(ledger.epoch_start_round, ledger.window_start_round)
    if detect_no_progress(rounds, window=ledger.stall_window, start=start):
        return (
            "PEEK",
            f"{reason}; clean-mode review shows no progress "
            f"(new findings per round {curve}) — check convergence",
        )
    if rec != "WAIT" and _is_falling(counts):
        return (
            "WAIT",
            f"{reason}; clean-mode review converging (new findings per round {curve})",
        )
    return None
