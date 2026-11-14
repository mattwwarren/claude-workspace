"""Shared decision core for recording engine-round SHOULD_FIX defers (#3636).

Two lanes write the same dev-queue settlement row: the CLI's
``cw review converge record-round`` (:mod:`cw.cli.review.converge_engine_defers`)
and the codex fix loop's ledger seams (:mod:`cw.codex_engine_defers`). Both call
:func:`record_settlement_best_effort` so there is one decision point for which
fingerprints settle and one best-effort write.

It is best-effort and never raises. Its only effect on failure is one WARNING,
and the failure direction is "no engine-backed attestation", which is today's
behavior, never a false PASS. It imports no ``cw.cli`` module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from cw.dev_queue.review_engine_reconcile import GATING_SEVERITIES, SHOULD_FIX
from cw.dev_queue.review_engine_settlements import record_engine_settlement
from cw.exceptions import CwError
from cw.review_engine.route import is_non_deferrable_rule

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from cw.review_convergence import RoundFinding

_log = logging.getLogger(__name__)

_WARNING: Final = (
    "review engine settlement not recorded (no identity, no REVIEW-stage "
    "RUNNING queue row, or the write failed): client=%s ticket=%s head=%s"
)


def deferred_fingerprints(findings: Sequence[RoundFinding]) -> list[str]:
    """The fingerprints to record, or ``[]`` unless the round is fully deferrable.

    Every gating finding must be a SHOULD_FIX that is ``defer``, carries a
    fingerprint and is not a coverage or acceptance rule. One MUST_FIX, one
    ``fix`` or one non-deferrable rule means nothing is recorded, because a
    partial record could never reconcile and writing nothing is fail-closed.
    """
    fingerprints: list[str] = []
    for finding in findings:
        if finding.severity not in GATING_SEVERITIES:
            continue
        if (
            finding.severity != SHOULD_FIX
            or finding.outcome != "defer"
            or finding.fingerprint is None
            or is_non_deferrable_rule(finding.rule_id)
        ):
            return []
        fingerprints.append(finding.fingerprint)
    return fingerprints


def warn_not_recorded(client: str | None, ticket: str | None, head: str) -> None:
    """Log the one WARNING a settlement that could not be recorded produces."""
    _log.warning(_WARNING, client, ticket, head)


def record_settlement_best_effort(
    ticket_id: str,
    client_name: str,
    head_sha: str,
    fingerprints: Sequence[str],
    *,
    recorded_at: datetime,
) -> None:
    """Write the settlement row for ``head_sha``, or log one WARNING. Never raises."""
    try:
        record_engine_settlement(
            ticket_id,
            client_name,
            head_sha,
            fingerprints,
            recorded_at=recorded_at,
        )
    except (CwError, OSError):
        warn_not_recorded(client_name, ticket_id, head_sha)
