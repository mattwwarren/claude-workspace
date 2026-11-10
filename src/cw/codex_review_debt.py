"""Record the codex lane's SHOULD_FIX deferrals in the session artifact (#3137).

The codex loop round and the Claude confirm round stamp SHOULD_FIX findings as
``defer`` on the convergence ledger, where they are non-gating. Only ``cw review
adjudicate`` and the engine-confirm path reach the review-debt filer, so a codex
defer needs a durable record that finalize can find.

:func:`file_codex_deferrals` writes the round's new defers to the session
artifact (``.cw/deferred-findings.md``) and files nothing. Filing happens once
per ticket, at finalize Step 4d item 2a (``cw review debt file``), from the
artifact's final state: a reviewer rewords the same finding from round to
round, so a per-round filer opened a ticket per wording (#2682 got 39 issues
over six rounds). It runs in the detached ``cw codex run`` process, whose cwd
is not guaranteed to be the session worktree, so every path it touches is
anchored on *worktree*.

Contract: *ledger* is the PRE-record ledger, the one ``record_round`` received.
``record_round`` settles every new defer into its returned ledger, so a
post-record ledger would make the "not already settled" filter drop them all.

Writing never raises: a failure is logged and the loop carries on, because the
defer is already recorded on the ledger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    Adjudication,
    merge_deferred_adjudications,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.review_convergence import finding_identity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundFinding

_log = logging.getLogger(__name__)


def round_deferral(finding: RoundFinding, round_number: int, now: str) -> Adjudication:
    """The debt ``Adjudication`` of one round-recorded SHOULD_FIX defer.

    Shared by the codex lane and ``cw review converge record-round``. A round
    records no line or evidence, so the identity is the file and the summary
    alone. The rationale is the finding's own (the lane's text, not a fixed one).
    """
    return Adjudication(
        severity=finding.severity,
        file=finding.file,
        line_start=None,
        line_end=None,
        evidence="",
        summary=finding.summary,
        outcome=DEFERRED_OUTCOME,
        rationale=finding.rationale,
        round=round_number,
        recorded_at=now,
    )


def _new_defers(
    findings: Sequence[RoundFinding], ledger: ConvergenceLedger
) -> list[RoundFinding]:
    """The round's defers that the pre-record ledger had not already settled."""
    settled = {entry.identity for entry in ledger.settled}
    return [
        finding
        for finding in findings
        if finding.outcome == DEFERRED_OUTCOME
        and finding_identity(
            finding.severity,
            finding.file,
            finding.summary,
            fingerprint=finding.fingerprint,
        )
        not in settled
    ]


def _write_artifact(path: Path, applied: Sequence[Adjudication]) -> None:
    """Merge *applied* into the session artifact, then write it atomically.

    The artifact is what finalize Step 4d item 2a reads to file the ticket's
    review debt once, so every round keeps it current.
    """
    prior = read_deferred_findings_md(path)
    merged = merge_deferred_adjudications(prior, list(applied))
    rendered = render_deferred_findings_md(merged)
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)


def file_codex_deferrals(
    *,
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    sid: str,
    findings: Sequence[RoundFinding],
    ledger: ConvergenceLedger,
    round_number: int,
    now: str,
) -> None:
    """Write the round's new defers to the session artifact; file nothing.

    Returns before any IO when nothing is new. The name is historical: the
    review-debt filer runs at finalize, not here.
    """
    new_defers = _new_defers(findings, ledger)
    if not new_defers:
        return
    applied = [round_deferral(f, round_number, now) for f in new_defers]
    try:
        _write_artifact(worktree / DEFERRED_FINDINGS_RELATIVE_PATH, applied)
    except (OSError, ValueError, CwError):
        _log.warning(
            "codex review-debt artifact write failed ticket=%s client=%s "
            "session=%s round=%s",
            task.ticket_id,
            client.name,
            sid,
            round_number,
            exc_info=True,
        )
        return
    _log.info(
        "codex review-debt deferrals recorded in artifact ticket=%s client=%s "
        "session=%s round=%s count=%d",
        task.ticket_id,
        client.name,
        sid,
        round_number,
        len(applied),
    )
