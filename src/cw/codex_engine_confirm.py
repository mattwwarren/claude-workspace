"""The codex confirm round after a repo review engine pass (RFC 0018 A4, #3216).

RFC 0017 I3 on the codex lane: once an engine loop round is clean, the
independent confirm round is decided in code
(:func:`cw.review_convergence.decide_confirm_verdict`). When it parks
(``stop_confirm_must_fix``), :func:`engine_confirm_park` renames the blocker to
``review_confirm_must_fix`` (never retryable, released by
``cw dev-queue approve --override-must-fix``) and records the same
``review.confirm_escalated`` audit event claude-native's
``announce_confirm_park`` records. A background run has no stderr, so the
friction line is a log warning instead.

Leaf module: it never imports :mod:`cw.codex_clean_confirm`, which imports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON
from cw.codex_review_signals import emit_confirm_escalated
from cw.review_convergence import (
    DECISION_STOP_CONFIRM_MUST_FIX,
    confirm_escalated_event_payload,
    one_line,
)

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundReport
    from cw.review_findings import ReviewVerdict

_log = logging.getLogger(__name__)


def pending_lines(verdict: ReviewVerdict) -> list[str]:
    """``SEVERITY file:line summary`` for each accepted MUST_FIX/SHOULD_FIX."""
    lines: list[str] = []
    for item in verdict.accepted:
        finding = item.finding
        if finding.severity not in ("MUST_FIX", "SHOULD_FIX"):
            continue
        where = finding.file
        if finding.line_start is not None:
            where = f"{where}:{finding.line_start}"
        lines.append(f"{finding.severity} {where} {one_line(finding.summary)}")
    return lines


def engine_confirm_park(
    result: AutoDevResult,
    report: RoundReport,
    ledger: ConvergenceLedger,
    task: TicketTask,
    client: ClientConfig,
) -> AutoDevResult:
    """*result* parked as ``review_confirm_must_fix`` on an engine-confirm stop.

    Only for ``stop_confirm_must_fix`` (which the ledger reaches only after an
    engine pass); *ledger* is the post-record ledger. The blocker keeps its
    details and release hint. Any other decision returns *result* unchanged.
    """
    blocker = result.blocker
    if report.decision != DECISION_STOP_CONFIRM_MUST_FIX or blocker is None:
        return result
    _log.warning(
        "codex confirm escalated: ticket=%s client=%s reason=%s round=%d",
        task.ticket_id,
        client.name,
        report.reason,
        report.round,
    )
    emit_confirm_escalated(
        confirm_escalated_event_payload(
            ledger, ticket_id=task.ticket_id, client=client.name
        ),
        ticket_id=task.ticket_id,
    )
    parked = blocker.model_copy(
        update={
            "reason": REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON,
            "retry_eligible": False,
        }
    )
    return result.model_copy(update={"blocker": parked})
