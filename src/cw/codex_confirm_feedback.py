"""Feed a dirty Claude confirm round back into the codex fix loop (#2807).

On a clean-mode codex lane (``review.convergence: clean``) the Claude confirm
round (:mod:`cw.codex_clean_confirm`) used to park on any MUST_FIX it found:
the codex fix loop never saw those findings, so a requeue re-ran a loop that
could park on the same finding again. This module turns the confirm round's
persisted verdict into seeds for the next loop
(``run_review_with_fix_loop(seed_findings=...)``), read by
``cw.codex_background._review_and_confirm`` at two points:

- before the first loop of a run (a requeue after a dirty confirm), and
- after each gate call that parked on a Claude MUST_FIX (the in-process
  re-entry, :func:`reenter_feedback`).

Both read through :func:`load_confirm_feedback`, so the two paths share one
code path. The gate itself is not touched: it already records the dirty round
(``fix_cycles_used`` + 1, status ``in_progress``) and persists the raw Claude
verdict as the round's verdict file. Everything here is gated on
:func:`reentry_enabled`, the single read site of ``review.confirm_reentry``:
with it off (or in blocking mode, or with the lane's fix loop off) no ledger
or verdict file is read and the run parks exactly as before.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from cw._git import capture_head_sha
from cw.auto_dev_result import AutoDevResult
from cw.claude_review import CLAUDE_REVIEWER_LABEL
from cw.codex_clean_confirm import _BOOKKEEPING_ERRORS
from cw.codex_fix_loop import FIX_CYCLE_FLOOR_SECONDS
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.models import CODEX_BACKEND
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    load_ledger,
    load_round_verdict,
    next_action,
    resolve_diff_base,
    suppress_settled,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.codex_clean_confirm import CleanConfirmOutcome
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

#: Dispositions of an accepted MUST_FIX that still need a fix: ``fixed`` is
#: the optimistic default, and ``dropped``/``unresolved`` mean nobody fixed
#: it. Mirrors ``cw.codex_clean_confirm._decided``, which records exactly
#: these as ``fix`` on the confirm round.
_OPEN_DISPOSITIONS = frozenset({"fixed", "dropped", "unresolved"})

_MUST_FIX = "MUST_FIX"

#: The friction line's prefix, one line per seeded loop entry.
_FRICTION_PREFIX = "claude_confirm_feedback"


class ConfirmFeedback(NamedTuple):
    """The dirty confirm round to feed back: its number and its MUST_FIX seeds."""

    round: int
    findings: tuple[AcceptedFinding, ...]


def reentry_enabled(client: ClientConfig, *, fix_loop_enabled: bool) -> bool:
    """Whether confirm findings re-enter the codex fix loop for *client*.

    Clean mode, ``review.confirm_reentry`` on, and the lane's fix loop
    enabled (the loop it re-enters). The one read site of the key.
    """
    return client.review.is_clean and client.review.confirm_reentry and fix_loop_enabled


def _dirty_confirm_round(
    ledger: ConvergenceLedger,
    *,
    task: TicketTask,
    head: str,
    fork_point: str,
    after_round: int,
) -> int | None:
    """The last round's number when it is a dirty confirm to fix at *head*.

    Same identity checks as ``cw.codex_clean_confirm._resumable`` (ticket,
    fork point, codex loop backend); ``next_action == "fix"`` means the ledger
    is open and its last round is dirty at HEAD.
    """
    if (
        ledger.ticket_id != task.ticket_id
        or ledger.fork_point != fork_point
        or ledger.loop_backend != CODEX_BACKEND
        or next_action(ledger, head_sha=head) != "fix"
    ):
        return None
    last = ledger.rounds[-1]
    if last.kind != "confirm" or last.new_fix <= 0 or last.round <= after_round:
        return None
    return last.round


def _open_must_fix(
    ledger: ConvergenceLedger, verdict: ReviewVerdict
) -> tuple[AcceptedFinding, ...]:
    """*verdict*'s accepted MUST_FIX findings still needing a fix.

    Operator-settled findings are stamped ``rejected``/``deferred`` by
    :func:`suppress_settled` first and so drop out. SHOULD_FIX never seeds
    (it never gates on this lane) and ``operator_actionable`` is not loop work.
    """
    suppressed, _adjudications = suppress_settled(ledger, verdict)
    return tuple(
        item
        for item in suppressed.accepted
        if item.finding.severity == _MUST_FIX and item.disposition in _OPEN_DISPOSITIONS
    )


def _read_feedback(
    *, task: TicketTask, worktree: Path, client: ClientConfig, after_round: int
) -> ConfirmFeedback | None:
    ledger_path = worktree / LEDGER_RELATIVE_PATH
    ledger = load_ledger(ledger_path)
    if ledger is None:
        return None
    round_number = _dirty_confirm_round(
        ledger,
        task=task,
        head=capture_head_sha(worktree),
        fork_point=resolve_diff_base(worktree, client.default_branch),
        after_round=after_round,
    )
    if round_number is None:
        return None
    findings = _open_must_fix(ledger, load_round_verdict(ledger_path, round_number))
    if not findings:
        return None
    return ConfirmFeedback(round=round_number, findings=findings)


def load_confirm_feedback(
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    fix_loop_enabled: bool,
    after_round: int = 0,
) -> ConfirmFeedback | None:
    """The last dirty Claude confirm round's MUST_FIX findings, or ``None``.

    ``None`` without any I/O when :func:`reentry_enabled` is false. Otherwise
    a result only when the ledger matches the ticket and fork point, was
    written by the codex loop, its next action at HEAD is ``fix``, and its
    last round is a confirm round with ``new_fix > 0`` numbered above
    *after_round* (the caller's last consumed round, so a seeded loop that
    parks without recording a round never re-triggers). An unreadable ledger,
    verdict file or git state logs a WARNING and returns ``None``: the run
    then parks or loops exactly as it did before this feature.
    """
    if not reentry_enabled(client, fix_loop_enabled=fix_loop_enabled):
        return None
    try:
        return _read_feedback(
            task=task, worktree=worktree, client=client, after_round=after_round
        )
    except _BOOKKEEPING_ERRORS:
        _log.warning(
            "claude confirm feedback unreadable, not re-entering the codex fix "
            "loop ticket=%s client=%s",
            task.ticket_id,
            client.name,
            exc_info=True,
        )
        return None


def _parked_on_claude_must_fix(gated: CleanConfirmOutcome) -> bool:
    """The gate parked on the Claude confirm round's own MUST_FIX findings."""
    blocker = gated.result.blocker
    return (
        gated.reviewer_label == CLAUDE_REVIEWER_LABEL
        and blocker is not None
        and blocker.reason == CODEX_MUST_FIX_FINDINGS
    )


def reenter_feedback(
    gated: CleanConfirmOutcome,
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    fix_loop_enabled: bool,
    after_round: int,
    deadline: float | None,
) -> ConfirmFeedback | None:
    """Feedback for an in-process re-entry after the gate, or ``None``.

    Cheap checks first (the key, then the outcome: a Claude-labeled
    ``codex_must_fix_findings`` park), so a converged, blocking or codex
    outcome never touches the ledger or the clock. Then the reader, then the
    deadline: with a wall-clock budget, a re-entry is refused when less than
    :data:`~cw.codex_fix_loop.FIX_CYCLE_FLOOR_SECONDS` remain, and the run
    keeps the gate's park.
    """
    if not reentry_enabled(client, fix_loop_enabled=fix_loop_enabled):
        return None
    if not _parked_on_claude_must_fix(gated):
        return None
    feedback = load_confirm_feedback(
        task=task,
        worktree=worktree,
        client=client,
        fix_loop_enabled=fix_loop_enabled,
        after_round=after_round,
    )
    if feedback is None:
        return None
    if deadline is not None and deadline - time.monotonic() < FIX_CYCLE_FLOOR_SECONDS:
        _log.warning(
            "claude confirm feedback not re-entered: wall-clock budget below the "
            "fix-cycle floor ticket=%s client=%s round=%s",
            task.ticket_id,
            client.name,
            feedback.round,
        )
        return None
    return feedback


def feedback_token(feedback: ConfirmFeedback) -> str:
    """The friction token for one seeded loop entry."""
    return (
        f"{_FRICTION_PREFIX}: round={feedback.round} findings={len(feedback.findings)}"
    )


def with_feedback_tokens(result: AutoDevResult, tokens: Sequence[str]) -> AutoDevResult:
    """*result* with *tokens* appended to its friction, re-validated.

    Appended to the FINAL gated result by the caller: a dirty-confirm park is
    the Claude round's own result, which drops anything added to the loop
    result before the gate. Re-validated the way the gate's ``_finish`` does.
    *result* itself when there are no tokens.
    """
    if not tokens:
        return result
    merged = result.model_copy(
        update={"friction_highlights": [*result.friction_highlights, *tokens]}
    )
    return AutoDevResult.model_validate(merged.model_dump(mode="json"))
