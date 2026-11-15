"""The engine-settlement seam of ``cw review converge record-round`` (GitHub #3625).

:func:`record_engine_defers` records the deferred SHOULD_FIX fingerprints of an
engine loop round on the dev-queue row (:mod:`cw.dev_queue.review_engine_settlements`),
so the review attestation can reconcile the FAIL summary the round left at its
head (:mod:`cw.dev_queue.review_engine_reconcile`).

The decision core (which fingerprints settle, and the best-effort write) lives in
:mod:`cw.dev_queue.review_engine_defers` and is shared with the codex lane
(:mod:`cw.codex_engine_defers`, #3636). This module keeps only the CLI's payload
gate and identity resolution.

It is best-effort and never raises. Its only effect on failure is one WARNING,
and the failure direction is "no engine-backed attestation", which is today's
behavior, never a false PASS. ``converge.py`` calls it on the line after
``file_round_deferrals``, before the ledger commit point. It registers no command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.cli.review.converge_confirm import round_ran_engine
from cw.cli.review.debt import resolve_identity
from cw.dev_queue.review_engine_defers import (
    deferred_fingerprints,
    record_settlement_best_effort,
    warn_not_recorded,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence import RoundFinding, RoundInput


def record_engine_defers(
    payload: RoundInput,
    findings: Sequence[RoundFinding],
    client: str | None,
    ticket: str | None,
    *,
    now: str,
) -> None:
    """Record an all-deferred engine round's SHOULD_FIX fingerprints, or nothing.

    Silent for any round the engine did not run alone, for a round with nothing
    to record, and for a round that is not fully deferrable. Only an unresolved
    identity, a refused row or a failed save logs its one WARNING. The exit code
    never changes.
    """
    if not round_ran_engine(payload):
        return
    fingerprints = deferred_fingerprints(findings)
    if not fingerprints:
        return
    identity = resolve_identity(client, ticket)
    if identity is None:
        warn_not_recorded(client, ticket, payload.reviewed_sha)
        return
    record_settlement_best_effort(
        identity.ticket,
        identity.client,
        payload.reviewed_sha,
        fingerprints,
        recorded_at=datetime.fromisoformat(now).astimezone(UTC),
    )
