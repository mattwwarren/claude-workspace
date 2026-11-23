"""The Claude-native clean-mode ledger verdict reader for the regress rule (#3862).

A clean-mode Claude-native REVIEW pass records one ``round-<n>-verdict.json``
per round beside the convergence ledger (``.cw/review-convergence.json``). The
codex lane's ``.claude/review-verdict.json`` does not exist on this lane, so the
regress rule reads the ledger instead. This module owns that read: which round's
verdict to deliver, whether the row owns the ledger, and whether the round's
head is the one the verdict names.

Only rounds the ledger lists are read: an orphan round file past the last
listed round (written just before a crash, never committed) is never read.
Imports are limited to ``cw.review_convergence``, ``cw.review_findings`` and the
standard library, so this module never reaches a ``cw.codex_*`` package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    load_ledger,
    load_round_verdict,
    suppress_settled,
)
from cw.review_findings import has_held_findings

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger, RoundRecord
    from cw.review_findings import ReviewVerdict

#: Refusals the reader can name. The strings equal the regress rule's
#: ``WithheldReason`` values; they are repeated here so this module does not
#: import ``impl_regress_findings`` (that would be a cycle).
LedgerRefusal = Literal["artifact_missing", "not_owned", "not_blocking"]

ARTIFACT_MISSING: Final[LedgerRefusal] = "artifact_missing"
NOT_OWNED: Final[LedgerRefusal] = "not_owned"
NOT_BLOCKING: Final[LedgerRefusal] = "not_blocking"


class LedgerVerdictRead(NamedTuple):
    """Outcome of :func:`read_held_round_verdict`: a verdict, or a refusal."""

    verdict: ReviewVerdict | None
    refusal: LedgerRefusal | None


def read_held_round_verdict(worktree: Path, ticket_id: str) -> LedgerVerdictRead:
    """Select the ledger round verdict the regress rule may deliver from.

    Refusals: ``artifact_missing`` (no ledger, unreadable ledger, no rounds, or
    no listed round file readable), ``not_owned`` (the ledger belongs to another
    ticket, or the delivered candidate's verdict names another head), and
    ``not_blocking`` (no listed round holds a held finding, or a listed round
    holds an unsettled decision contest that must withhold).
    """
    ledger_path = worktree / LEDGER_RELATIVE_PATH
    try:
        ledger = load_ledger(ledger_path)
    except (OSError, ValueError):
        return LedgerVerdictRead(None, ARTIFACT_MISSING)
    if ledger is None or not ledger.rounds:
        return LedgerVerdictRead(None, ARTIFACT_MISSING)
    if ledger.ticket_id != ticket_id:
        return LedgerVerdictRead(None, NOT_OWNED)
    return _latest_held_round(ledger_path, ledger)


def _round_verdict(ledger_path: Path, record: RoundRecord) -> ReviewVerdict | None:
    """The listed round's saved verdict, or ``None`` when it has no readable file."""
    try:
        return load_round_verdict(ledger_path, record.round)
    except (OSError, ValueError):
        return None


def _latest_held_round(
    ledger_path: Path, ledger: ConvergenceLedger
) -> LedgerVerdictRead:
    """Scan listed rounds newest first: contest wins, else the newest held round.

    The scan keeps going past the candidate only to ask whether ANY listed round
    holds an unsettled decision contest; a contest returns that round's verdict
    at once so the rule withholds. No findings are unioned across rounds.
    """
    candidate: tuple[ReviewVerdict, RoundRecord] | None = None
    readable = False
    for record in reversed(ledger.rounds):
        verdict = _round_verdict(ledger_path, record)
        if verdict is None:
            continue
        readable = True
        settled, _ = suppress_settled(ledger, verdict)
        if has_held_findings(settled, "decision_contest"):
            return LedgerVerdictRead(settled, None)
        if candidate is None and has_held_findings(settled):
            candidate = (settled, record)
    if candidate is None:
        return LedgerVerdictRead(None, NOT_BLOCKING if readable else ARTIFACT_MISSING)
    return _pin_candidate(*candidate)


def _pin_candidate(verdict: ReviewVerdict, record: RoundRecord) -> LedgerVerdictRead:
    """Refuse a delivered candidate whose verdict names another head than its round."""
    if verdict.reviewed_sha != record.reviewed_sha:
        return LedgerVerdictRead(None, NOT_OWNED)
    return LedgerVerdictRead(verdict, None)
