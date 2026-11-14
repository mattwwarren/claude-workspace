"""The engine-settlement seam of ``cw review converge record-round`` (GitHub #3625).

:func:`record_engine_defers` records the deferred SHOULD_FIX fingerprints of an
engine loop round on the dev-queue row (:mod:`cw.dev_queue.review_engine_settlements`),
so the review attestation can reconcile the FAIL summary the round left at its
head (:mod:`cw.dev_queue.review_engine_reconcile`).

It is best-effort and never raises. Its only effect on failure is one WARNING,
and the failure direction is "no engine-backed attestation", which is today's
behavior, never a false PASS. ``converge.py`` calls it on the line after
``file_round_deferrals``, before the ledger commit point. It registers no command.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.cli.review.converge_confirm import round_ran_engine
from cw.cli.review.debt import resolve_identity
from cw.dev_queue.review_engine_settlements import record_engine_settlement
from cw.exceptions import CwError
from cw.review_engine.route import is_non_deferrable_rule

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence import RoundFinding, RoundInput

_log = logging.getLogger(__name__)

_GATING: Final = frozenset({"MUST_FIX", "SHOULD_FIX"})
_WARNING: Final = (
    "review engine settlement not recorded (no identity, no REVIEW-stage "
    "RUNNING queue row, or the write failed): client=%s ticket=%s head=%s"
)


def _deferred_fingerprints(findings: Sequence[RoundFinding]) -> list[str]:
    """The fingerprints to record, or ``[]`` unless the round is fully deferrable.

    Every gating finding must be a SHOULD_FIX that is ``defer``, carries a
    fingerprint and is not a coverage or acceptance rule. One MUST_FIX, one
    ``fix`` or one non-deferrable rule means nothing is recorded, because a
    partial record could never reconcile and writing nothing is fail-closed.
    """
    fingerprints: list[str] = []
    for finding in findings:
        if finding.severity not in _GATING:
            continue
        if (
            finding.severity != "SHOULD_FIX"
            or finding.outcome != "defer"
            or finding.fingerprint is None
            or is_non_deferrable_rule(finding.rule_id)
        ):
            return []
        fingerprints.append(finding.fingerprint)
    return fingerprints


def _warn(client: str | None, ticket: str | None, head: str) -> None:
    _log.warning(_WARNING, client, ticket, head)


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
    fingerprints = _deferred_fingerprints(findings)
    if not fingerprints:
        return
    identity = resolve_identity(client, ticket)
    if identity is None:
        _warn(client, ticket, payload.reviewed_sha)
        return
    recorded_at = datetime.fromisoformat(now).astimezone(UTC)
    try:
        record_engine_settlement(
            identity.ticket,
            identity.client,
            payload.reviewed_sha,
            fingerprints,
            recorded_at=recorded_at,
        )
    except (CwError, OSError):
        _warn(identity.client, identity.ticket, payload.reviewed_sha)
