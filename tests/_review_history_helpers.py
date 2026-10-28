"""Shared builders for the ``cw review history`` tests (#2790).

Holds what ``test_review_history.py``, ``test_review_history_render.py`` and
``test_cli_review_history.py`` share. No ``test_`` prefix, so pytest does not
collect it (same convention as ``tests/_review_convergence_helpers.py``).

Every ledger is built THROUGH the real engine (``record_round``,
``operator_settle``, ``reconcile_ledger_for_init``), never a hand-typed dict,
and every ``review.finding_voided`` event is emitted by the real
``apply_voided_suppression``; only the deliberately foreign event of the
cross-client tests is recorded directly, through the real ``record_event``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from freezegun import freeze_time

from cw.events import record_event
from cw.models import ClientConfig, OrchestratorEventType, QueueItemStatus, Stage
from cw.models.enums import StageIdentifier
from cw.review_adjudication import VoidedFinding, apply_voided_suppression
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    SettleTarget,
    from_verdict,
    operator_settle,
    reconcile_ledger_for_init,
    record_round,
    save_round_verdict,
)
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from tests._review_convergence_helpers import (
    LOOP_BACKEND,
    NOW,
    ROSTER,
    consolidated_verdict,
    fresh_ledger,
    record,
    round_finding,
    write_ledger,
)
from tests._ticket_view_helpers import ACME, ACME_TICKET, bound_session_for, make_task

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cw.models import OrchestratorEvent, Session, TicketTask
    from cw.review_convergence import RoundKind
    from cw.review_findings import ReviewVerdict, Severity

#: Round ``recorded_at`` stamps the fixtures apply through :func:`timed`.
STAMPS = (
    "2026-10-27T12:00:00+00:00",
    "2026-10-27T12:12:04+00:00",
    "2026-10-27T12:20:00+00:00",
    "2026-10-27T12:30:00+00:00",
    "2026-10-27T12:35:00+00:00",
)
#: When this row's review session entered Stage 3 (10 minutes before round 1).
REVIEW_START = "2026-10-27T11:50:00+00:00"
#: The session id the acme row is bound to.
ACME_SESSION = "sess-acme"
#: The other client sharing ticket id ``ACM-1`` in the cross-client tests.
OTHER = "other"
OTHER_SESSION = "sess-other"
OTHER_FILE = "src/other_client/secret.py"
OTHER_SUMMARY = "Other client's private finding text"
OTHER_COMMENT = "someone-else@2026-10-01T00:00:00Z"
OTHER_RATIONALE = "other client's private rejection reason"

COMMENT_ID = "mattwwarren@2026-10-20T09:00:00Z"
VOIDED_AT = "2026-10-20T09:05:00Z"
ORIGINAL_RATIONALE = "intentional tradeoff, see ADR-0012"
VOIDED_SUMMARY = "Voided by the operator"


def client_config(tmp_path: Path, name: str = ACME) -> ClientConfig:
    """A configured client whose derived worktrees live under ``tmp_path/wt``."""
    return ClientConfig(
        name=name, workspace_path=tmp_path / "ws", worktree_base=tmp_path / "wt"
    )


def derived_worktree(tmp_path: Path) -> Path:
    """Where :func:`client_config`'s client puts ``ACM-1``'s worktree."""
    return tmp_path / "wt" / f"dev-{ACME_TICKET}"


def timed(ledger: ConvergenceLedger, *stamps: str) -> ConvergenceLedger:
    """*ledger* with each round's ``recorded_at`` replaced by *stamps*, in order.

    ``record()`` stamps every round with one fixed ``NOW``; wall-time tests
    need distinct times. Fewer stamps than rounds leaves the tail unchanged.
    """
    rounds = [
        r.model_copy(update={"recorded_at": stamps[i]}) if i < len(stamps) else r
        for i, r in enumerate(ledger.rounds)
    ]
    return ledger.model_copy(update={"rounds": rounds})


def record_verdict_round(
    ledger: ConvergenceLedger,
    verdict: ReviewVerdict,
    sha: str,
    kind: RoundKind = "loop",
) -> tuple[ConvergenceLedger, ReviewVerdict]:
    """Record *verdict* as one round through the real engine (verdict form)."""
    findings, unread = from_verdict(verdict)
    backend = ledger.confirm_backend if kind == "confirm" else LOOP_BACKEND
    updated, _ = record_round(
        ledger,
        kind=kind,
        backend=backend,
        reviewed_sha=sha,
        findings=findings,
        roster=ROSTER,
        now=NOW,
        unread=unread,
    )
    return updated, verdict


def write_round_verdict(worktree: Path, n: int, verdict: ReviewVerdict) -> Path:
    """Persist *verdict* as round *n*'s verdict file beside the default ledger."""
    return save_round_verdict(worktree / LEDGER_RELATIVE_PATH, n, verdict)


def sentinel_review(
    review: Mapping[str, object] | None = None,
    *,
    friction: tuple[str, ...] = (),
    status: str = "shipped",
) -> dict[str, object]:
    """A raw ``Session.last_result`` dict carrying *review* and *friction*."""
    result: dict[str, object] = {
        "status": status,
        "friction_highlights": list(friction),
    }
    if review is not None:
        result["review"] = dict(review)
    return result


def stage_entered_event(
    ticket: str, session_id: str | None, at: str
) -> OrchestratorEvent:
    """Record ``stage.entered`` / ``s3_review_started`` at *at*, as the prose does.

    Same payload keys as ``auto-dev-review.md``; ``session_id=None`` omits the
    key entirely (the prose's ``$CW_SESSION`` unset).
    """
    payload: dict[str, object] = {
        "ticket_id": ticket,
        "stage": StageIdentifier.S3_REVIEW_STARTED.value,
        "prev_stage": StageIdentifier.S2_IMPL_COMPLETE.value,
        "started_at": at,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    with freeze_time(at):
        return record_event(
            OrchestratorEventType.STAGE_ENTERED, payload, correlation_id=ticket
        )


def voided_suppression(
    ticket: str,
    verdict: ReviewVerdict,
    *,
    summary: str = VOIDED_SUMMARY,
    comment_id: str = COMMENT_ID,
    original_rationale: str = ORIGINAL_RATIONALE,
    voided_at: str = VOIDED_AT,
) -> ReviewVerdict:
    """Suppress *verdict*'s finding *summary* through the real voided path.

    Builds the :class:`VoidedFinding` from the accepted finding's own identity
    fields, so the real matcher fires: the real ``review.finding_voided`` event
    is emitted and the finding is stamped ``rejected`` with the real voided
    rationale. Returns the stamped verdict, ready for
    :func:`record_verdict_round`.
    """
    accepted = next(a for a in verdict.accepted if a.finding.summary == summary)
    void = VoidedFinding(
        severity=accepted.finding.severity,
        file=accepted.finding.file,
        summary=summary,
        evidence=accepted.finding.evidence,
        operator_comment_id=comment_id,
        voided_at=voided_at,
        original_rationale=original_rationale,
    )
    stamped, adjudications = apply_voided_suppression(verdict, [void], ticket_id=ticket)
    assert len(adjudications) == 1
    return stamped


def write_voided_ledger(worktree: Path, ticket: str = ACME_TICKET) -> None:
    """Write *ticket*'s one-round ledger to *worktree*, a SHOULD_FIX voided in it."""
    verdict = consolidated_verdict(
        accepted=[("MUST_FIX", "Real bug"), ("SHOULD_FIX", VOIDED_SUMMARY)]
    )
    ledger, _ = record_verdict_round(
        fresh_ledger(ticket_id=ticket),
        voided_suppression(ticket, verdict),
        "sha-0001",
    )
    write_ledger(worktree, ledger)


def review_task(**overrides: object) -> TicketTask:
    """A completed row at the review stage; *overrides* go to ``make_task``."""
    kwargs: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.COMPLETED,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def foreign_voided_event(ticket: str, **payload: str) -> OrchestratorEvent:
    """A ``review.finding_voided`` event for a finding in no ledger of this row.

    Stands for another client's suppression of a ticket that shares the id:
    the real emitter carries no client, session or worktree, only
    ``correlation_id=ticket``.
    """
    body: dict[str, object] = {
        "file": OTHER_FILE,
        "severity": "SHOULD_FIX",
        "summary": OTHER_SUMMARY,
        "operator_comment_id": OTHER_COMMENT,
        "voided_at": VOIDED_AT,
        "original_rationale": OTHER_RATIONALE,
    }
    body.update(payload)
    return record_event(
        OrchestratorEventType.REVIEW_FINDING_VOIDED, body, correlation_id=ticket
    )


@dataclass(frozen=True)
class LedgerFixture:
    """A ledger plus the per-round verdicts its verdict-form rounds recorded."""

    ledger: ConvergenceLedger
    verdicts: dict[int, ReviewVerdict] = field(default_factory=dict)

    def write(self, worktree: Path, *, with_verdicts: bool = True) -> Path:
        """Persist the ledger (and, by default, its verdict files) in *worktree*."""
        path = write_ledger(worktree, self.ledger)
        if with_verdicts:
            for n, verdict in self.verdicts.items():
                write_round_verdict(worktree, n, verdict)
        return path


def _accepted(prefix: str, must: int, should: int) -> list[tuple[Severity, str]]:
    pairs: list[tuple[Severity, str]] = [
        ("MUST_FIX", f"{prefix} must {i}") for i in range(must)
    ]
    pairs.extend(("SHOULD_FIX", f"{prefix} should {i}") for i in range(should))
    return pairs


#: ``(must_fix, should_fix)`` new findings per loop round of the converged fixture.
CONVERGED_SPLITS = ((3, 2), (1, 2), (1, 1), (0, 0))


def fixture_converged(ticket: str = ACME_TICKET) -> LedgerFixture:
    """Loop rounds 5, 3, 2, 0 then a clean codex confirm: converged, verdict form."""
    ledger = fresh_ledger(confirm="codex", ticket_id=ticket)
    verdicts: dict[int, ReviewVerdict] = {}
    sha = ""
    for index, (must, should) in enumerate(CONVERGED_SPLITS):
        sha = f"sha-000{index + 1}"
        verdict = consolidated_verdict(
            accepted=_accepted(f"r{index + 1}", must, should), sha=sha
        )
        ledger, verdicts[index + 1] = record_verdict_round(ledger, verdict, sha)
    confirm = consolidated_verdict(sha=sha)
    ledger, verdicts[len(CONVERGED_SPLITS) + 1] = record_verdict_round(
        ledger, confirm, sha, kind="confirm"
    )
    assert ledger.status == "converged"
    return LedgerFixture(timed(ledger, *STAMPS), verdicts)


REJECT_RATIONALE = "not a bug: the guard is upstream"
DEFER_RATIONALE = "follow-up ticket filed"
OPERATOR_REASON = "operator rejected: recurring false positive"


def fixture_stopped(ticket: str = ACME_TICKET) -> LedgerFixture:
    """An oscillation stop, released once, then stopped again; findings form.

    Round 1 settles a reject and a defer (adjudication origin); round 2 trips
    oscillation on ``Recurring defect``, which the operator then settles
    (operator origin, ``round == 2``); a same-tip init releases the stop
    (``identities_settled``); round 3 trips oscillation again on ``Other bug``.
    """
    ledger = fresh_ledger(ticket_id=ticket)
    ledger, _ = record(
        ledger,
        round_finding(summary="Recurring defect"),
        round_finding(summary="Other bug"),
        round_finding(
            "SHOULD_FIX", "False alarm", outcome="reject", rationale=REJECT_RATIONALE
        ),
        round_finding(
            "SHOULD_FIX", "Later thing", outcome="defer", rationale=DEFER_RATIONALE
        ),
        sha="sha-0001",
    )
    ledger, _ = record(
        ledger, round_finding(summary="Recurring defect"), sha="sha-0002"
    )
    assert ledger.stop_reason == "oscillation"
    ledger, _ = operator_settle(
        ledger,
        [
            SettleTarget(
                severity="MUST_FIX", file="src/cw/foo.py", summary="Recurring defect"
            )
        ],
        actor="mattwwarren",
        reason=OPERATOR_REASON,
        now=NOW,
    )
    reconciled = reconcile_ledger_for_init(
        ledger,
        fresh=fresh_ledger(ticket_id=ticket),
        ticket_id=ticket,
        fork_point=ledger.fork_point,
        history_rewritten=False,
        head_sha="sha-0002",
        max_cycles=ledger.max_cycles,
        now=NOW,
    )
    assert reconciled.released is not None
    ledger, _ = record(
        reconciled.ledger, round_finding(summary="Other bug"), sha="sha-0003"
    )
    assert ledger.status == "stopped"
    assert ledger.stop_reason == "oscillation"
    return LedgerFixture(timed(ledger, *STAMPS))


def fixture_awaiting_confirm(ticket: str = ACME_TICKET) -> LedgerFixture:
    """A dirty loop round, then a clean one awaiting its codex confirm."""
    ledger = fresh_ledger(confirm="codex", ticket_id=ticket)
    ledger, _ = record(
        ledger, round_finding(summary="a"), round_finding(summary="b"), sha="sha-0001"
    )
    ledger, _ = record(ledger, sha="sha-0002")
    assert ledger.status == "awaiting_confirm"
    return LedgerFixture(timed(ledger, *STAMPS))


def fixture_in_progress(ticket: str = ACME_TICKET) -> LedgerFixture:
    """One dirty loop round with a fix cycle dispatched."""
    ledger = fresh_ledger(ticket_id=ticket)
    ledger, _ = record(
        ledger, round_finding(summary="a"), round_finding(summary="b"), sha="sha-0001"
    )
    assert ledger.status == "in_progress"
    return LedgerFixture(timed(ledger, *STAMPS))


def fixture_zero_round(ticket: str = ACME_TICKET) -> LedgerFixture:
    """A freshly initialized ledger with no recorded round."""
    return LedgerFixture(fresh_ledger(ticket_id=ticket))


BLOCKING_REVIEW: dict[str, object] = {
    "must_fix_initial": 3,
    "should_fix": 2,
    "fix_cycles_used": 2,
    "deferred": 1,
}
REJECTED_SUMMARY = "Rejected by the operator"
REVERSED_SUMMARY = "Settled, then withdrawn"


def blocking_dispositions() -> dict[str, FindingDisposition]:
    """Two row disposition records: a live REJECTED and a withdrawn REVERSED."""
    return {
        _disposition_key("src/cw/foo.py", REJECTED_SUMMARY) or "": FindingDisposition(
            outcome="REJECTED",
            rationale="false positive: covered by the caller",
            recorded_at="2026-10-26T10:00:00Z",
            actor="mattwwarren",
            reviewed_sha="abc1234",
            summary=REJECTED_SUMMARY,
        ),
        _disposition_key("src/cw/bar.py", REVERSED_SUMMARY) or "": FindingDisposition(
            outcome="REVERSED",
            rationale="withdrawn: it was a real bug",
            recorded_at="2026-10-26T11:00:00Z",
            actor="mattwwarren",
            reviewed_sha="abc1234",
            summary=REVERSED_SUMMARY,
        ),
    }


def fixture_blocking_sentinel() -> dict[str, object]:
    """A blocking-mode sentinel: cycle-0 counts, no clean-mode fields."""
    return sentinel_review(BLOCKING_REVIEW)


def clean_sentinel_session(task: TicketTask, worktree: Path) -> Session:
    """A clean-mode session whose result carries two ``review_round_<n>`` lines.

    Its worktree is *worktree*, so with a corrupt ledger there the history is
    rebuilt from these friction lines (``sentinel_friction`` rounds, which
    record a settled total but no new/resurfaced split).
    """
    review = {"convergence_mode": "clean", "rounds_run": 2, "converged": True}
    friction = (
        "review_round_1: kind=loop backend=claude-native sha=abc1234 "
        "new_accepted=2 settled=1",
        "review_round_2: kind=loop backend=claude-native sha=def5678 "
        "new_accepted=0 settled=0",
    )
    return bound_session_for(
        task,
        id="sess-s",
        worktree_path=worktree,
        last_result=sentinel_review(review, friction=friction),
    )


def write_corrupt_ledger(worktree: Path) -> Path:
    """An unreadable ``.cw/review-convergence.json`` in *worktree*."""
    path = worktree / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path
