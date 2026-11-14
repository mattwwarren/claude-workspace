"""Codex-lane engine-defer settlement for the fix loop's ledger seams (#3636).

The codex lane's engine-only passes can end fully deferrable: every gating
SHOULD_FIX is non-blocking. Without a settlement row the review attestation
cannot reconcile the engine's FAIL summary at the head, so the lane never
produces an engine-backed PASS. These helpers decide which findings to defer
and write the row through the shared core (:mod:`cw.dev_queue.review_engine_defers`).

Both helpers are side-effect free on the ledger. :func:`engine_defer_findings`
returns the findings the ledger should settle; the caller decides whether to
record them. It is kept out of :mod:`cw.codex_clean_confirm` so that module does
not grow further, and it imports neither ``codex_clean_confirm`` nor
``codex_loop_rounds`` (the rationale is passed in to avoid a cycle).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue.review_engine_defers import (
    deferred_fingerprints,
    record_settlement_best_effort,
)
from cw.review_convergence import (
    ConvergenceError,
    decide_verdict,
    from_verdict,
    verdict_ran_engine,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import RoundFinding
    from cw.review_findings import ReviewVerdict


def engine_defer_findings(verdict: ReviewVerdict, rationale: str) -> list[RoundFinding]:
    """The findings a final engine clean pass should settle as defers, or ``[]``.

    Returns the findings only when the pass ran the engine alone *and* every
    gating finding is a deferrable SHOULD_FIX. That is the exact condition under
    which the settlement row is written, so ledger content never changes unless
    the row write is attempted. Any other pass returns ``[]`` and stays
    byte-identical to today.
    """
    if not verdict_ran_engine(verdict):
        return []
    try:
        findings, _unread = from_verdict(decide_verdict(verdict, rationale))
    except ConvergenceError:
        return []
    if not deferred_fingerprints(findings):
        return []
    return findings


def record_codex_engine_defers(
    *,
    task: TicketTask,
    client: ClientConfig,
    head: str,
    findings: list[RoundFinding],
    engine: bool,
) -> None:
    """Write the settlement row for an engine round's defers, best-effort.

    Silent unless the round ran the engine alone and every finding is a
    deferrable SHOULD_FIX. Never raises: a refused row or a failed save logs one
    WARNING and the lane continues.
    """
    if not engine:
        return
    fingerprints = deferred_fingerprints(findings)
    if not fingerprints:
        return
    record_settlement_best_effort(
        task.ticket_id,
        client.name,
        head,
        fingerprints,
        recorded_at=datetime.now(UTC),
    )
