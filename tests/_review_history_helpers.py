"""Shared builders for the ``cw review history`` tests (#2790).

Holds what the ``test_review_history_{collect,ledger,sentinel,attribution,
build}.py`` files (one per ``cw.review_history`` submodule),
``test_review_history_render.py`` and ``test_cli_review_history.py`` share
(including the ``history_for`` / ``session_for`` / ``notes_with`` /
``running_task`` builders and the two "could not be tied" notes). No ``test_``
prefix, so pytest does not collect it (same convention as
``tests/_review_convergence_helpers.py``).

Every ledger is built THROUGH the real engine (``record_round``,
``operator_settle``, ``reconcile_ledger_for_init``), never a hand-typed dict,
and every ``review.finding_voided`` event is emitted by the real
``apply_voided_suppression``; only the deliberately foreign event of the
cross-client tests is recorded directly, through the real ``record_event``.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from freezegun import freeze_time

from cw.dev_queue import transition_task_status
from cw.events import record_event
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.models.enums import SessionPurpose, SessionStatus, StageIdentifier
from cw.review_adjudication import VoidedFinding, apply_voided_suppression
from cw.review_adjudication._voided import _voided_rationale
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    SettleTarget,
    from_verdict,
    operator_settle,
    reconcile_ledger_for_init,
    record_confirm_failure,
    record_round,
    save_round_verdict,
)
from cw.review_finding_dispositions import FindingDisposition, _disposition_key
from cw.review_history import build_review_history, collect_history_inputs
from tests._review_convergence_helpers import (
    LOOP_BACKEND,
    NOW,
    ROSTER,
    consolidated_verdict,
    fresh_ledger,
    operator_finding,
    record,
    round_finding,
    write_ledger,
)
from tests._ticket_view_helpers import ACME, ACME_TICKET, bound_session_for, make_task

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from cw.models import OrchestratorEvent, Session, TicketTask
    from cw.models.review_history import ReviewHistory
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

#: The note for a ``stage.entered`` event that ties to none of the row's sessions.
STAGE_NOTE = (
    "1 stage.entered event(s) for ticket id 'ACM-1' could not be tied to this "
    "row's sessions; not used"
)
#: The note for a ``review.finding_voided`` event that ties to no record of the row.
VOIDED_NOTE = (
    "1 review.finding_voided event(s) for ticket id 'ACM-1' could not be tied "
    "to this row; not shown"
)
#: The note for a ``review.finding_voided`` event that matches a record of the
#: row by content only (the record's rationale names no operator comment).
CONTENT_ONLY_NOTE = (
    "1 review.finding_voided event(s) for ticket id 'ACM-1' match a record of "
    "this row by content only (its rationale names no operator comment); not "
    "shown, since another client's ticket with the same id cannot be ruled out"
)

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
    ticket: str,
    session_id: str | None,
    at: str,
    stage: str = StageIdentifier.S3_REVIEW_STARTED.value,
) -> OrchestratorEvent:
    """Record ``stage.entered`` for *stage* at *at*, as the prose does.

    *stage* defaults to ``s3_review_started``. Same payload keys as
    ``auto-dev-review.md``; ``session_id=None`` omits the key entirely (the
    prose's ``$CW_SESSION`` unset).
    """
    payload: dict[str, object] = {
        "ticket_id": ticket,
        "stage": stage,
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


def write_voided_ledger(
    worktree: Path,
    ticket: str = ACME_TICKET,
    *,
    comment_id: str = COMMENT_ID,
    original_rationale: str = ORIGINAL_RATIONALE,
) -> None:
    """Write *ticket*'s one-round ledger to *worktree*, a SHOULD_FIX voided in it.

    The settled entry carries the real voided rationale, which names
    *comment_id*; the real ``review.finding_voided`` event is emitted too.
    """
    verdict = consolidated_verdict(
        accepted=[("MUST_FIX", "Real bug"), ("SHOULD_FIX", VOIDED_SUMMARY)]
    )
    ledger, _ = record_verdict_round(
        fresh_ledger(ticket_id=ticket),
        voided_suppression(
            ticket,
            verdict,
            comment_id=comment_id,
            original_rationale=original_rationale,
        ),
        "sha-0001",
    )
    write_ledger(worktree, ledger)


def write_rejected_ledger(
    worktree: Path,
    ticket: str = ACME_TICKET,
    rationale: str = ORIGINAL_RATIONALE,
) -> None:
    """Write *ticket*'s one-round ledger, the voided finding rejected un-prefixed.

    Same identity as :func:`write_voided_ledger`'s finding (``src/cw/foo.py``,
    ``VOIDED_SUMMARY``), but settled by an ordinary reject: its rationale is
    *rationale*, which names no operator comment. Emits no event.
    """
    ledger, _ = record(
        fresh_ledger(ticket_id=ticket),
        round_finding(
            "SHOULD_FIX", VOIDED_SUMMARY, outcome="reject", rationale=rationale
        ),
        sha="sha-0001",
    )
    write_ledger(worktree, ledger)


def producer_rationale(comment_id: str = COMMENT_ID) -> str:
    """The rationale the real producer stamps on a finding voided by *comment_id*."""
    return _voided_rationale(
        VoidedFinding(
            severity="SHOULD_FIX",
            file="src/cw/foo.py",
            summary=VOIDED_SUMMARY,
            evidence="e",
            operator_comment_id=comment_id,
            voided_at=VOIDED_AT,
            original_rationale=ORIGINAL_RATIONALE,
        )
    )


def review_task(**overrides: object) -> TicketTask:
    """A completed row at the review stage; *overrides* go to ``make_task``."""
    kwargs: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.COMPLETED,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def running_task(**overrides: object) -> TicketTask:
    """A still-running row at the review stage; *overrides* go to ``make_task``."""
    return review_task(status=QueueItemStatus.RUNNING, **overrides)


def history_for(
    task: TicketTask,
    tmp_path: Path,
    *,
    sessions: Sequence[Session] = (),
    configured: bool = True,
    worktree: Path | None = None,
    client_cfg: ClientConfig | None = None,
    now: str | None = None,
    **extra: object,
) -> ReviewHistory:
    """Collect and build *task*'s history, as ``cw review history`` does.

    *now* freezes the clock around the collection, which is what stamps
    ``HistoryInputs.now`` (#3792); *extra* goes to ``collect_history_inputs``.
    """
    cfg = client_cfg or (client_config(tmp_path) if configured else None)
    with freeze_time(now) if now else nullcontext():
        inputs = collect_history_inputs(
            task,
            cfg,
            CwState(sessions=list(sessions)),
            worktree_override=worktree,
            **extra,
        )
    return build_review_history(inputs)


def session_for(task: TicketTask, sid: str, **overrides: object) -> Session:
    """A session of *task*'s client, id *sid*; *overrides* go to the session."""
    return bound_session_for(task, id=sid, **overrides)


def notes_with(history: ReviewHistory, needle: str) -> list[str]:
    """The notes of *history* that contain *needle*."""
    return [n for n in history.notes if needle in n]


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


def other_client_voided_event() -> None:
    """Fire another client's suppression of its own ticket ``ACM-1``.

    Emitted by the real ``apply_voided_suppression``, so the event's severity,
    file and summary equal acme's voided finding (``write_voided_ledger`` /
    ``write_rejected_ledger``); only the comment id and original rationale
    differ. The event carries no client, so content cannot tell the two apart.
    """
    verdict = consolidated_verdict(accepted=[("SHOULD_FIX", VOIDED_SUMMARY)])
    voided_suppression(
        ACME_TICKET,
        verdict,
        comment_id=OTHER_COMMENT,
        original_rationale=OTHER_RATIONALE,
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


#: Distinct causes, so the engine records each failure rather than one run.
CONFIRM_FAILURE_CAUSES = ("timeout", "auth", "rate_limited", "timeout")


def fixture_confirm_failed(ticket: str = ACME_TICKET) -> LedgerFixture:
    """An awaiting-confirm ledger with four confirm-backend outages on record.

    Built through the real ``record_confirm_failure``, which adds no round and
    leaves the ledger ``awaiting_confirm``.
    """
    ledger = fixture_awaiting_confirm(ticket).ledger
    for cause in CONFIRM_FAILURE_CAUSES:
        ledger, _ = record_confirm_failure(
            ledger, cause=cause, detail=f"{cause} from codex", now=NOW
        )
    assert ledger.status == "awaiting_confirm"
    assert len(ledger.confirm_failures) == len(CONFIRM_FAILURE_CAUSES)
    return LedgerFixture(ledger)


def fixture_operator_action(ticket: str = ACME_TICKET) -> LedgerFixture:
    """One loop round with two fix findings and one ``operator_action`` finding.

    ``new_accepted`` is 3 while ``new_fix`` is 2: the round's fix dispatch
    excludes the finding only an operator can act on.
    """
    ledger = fresh_ledger(ticket_id=ticket)
    ledger, _ = record(
        ledger,
        round_finding(summary="a"),
        round_finding(summary="b"),
        operator_finding(),
        sha="sha-0001",
    )
    record_view = ledger.rounds[0]
    assert (record_view.new_accepted, record_view.new_fix) == (3, 2)
    assert record_view.new_operator_action == 1
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


# --- stage timing (#3792) ----------------------------------------------------------

#: When the timing fixtures' dev-queue row was created (before every event).
ROW_CREATED = "2026-10-27T11:00:00+00:00"

#: The Public Strings of the plan, verbatim (field descriptions of the model).
STAGE_SECONDS_DOC = (
    "Seconds the review stage spent queued, reviewing, repairing and parked at "
    "a gate (#3792)."
)
STAGE_SECONDS_QUEUED = (
    "Seconds the row was PENDING at the review stage, waiting to be "
    "dispatched. In a round, counted only inside that round's `wall_seconds` "
    "window; in the summary, from entry to the review stage, so it includes "
    "the initial wait before round 1. Null when the recorded status "
    "transitions do not cover the window; 0.0 when they do and the row never "
    "waited."
)
STAGE_SECONDS_REVIEWING = (
    "Seconds the row was RUNNING at the review stage with no fix session "
    "running: the REVIEW session's boot, reviewers and adjudication, plus the "
    "latency between a fix handoff and the fix session's start. Null when the "
    "recorded status transitions do not cover the window; 0.0 when they do "
    "and the row spent no such time."
)
STAGE_SECONDS_REPAIRING = (
    "Seconds a fix session of this row was running, from the session's "
    "`started_at` to its `completed_at` (a still-live session is counted up "
    "to `summary.stage_seconds_as_of`). It starts at the fix session's "
    "`started_at`, not at the fix handoff. Null when a fix was dispatched but "
    "no fix session of this row overlaps the window; 0.0 when no fix was "
    "dispatched."
)
STAGE_SECONDS_GATE = (
    "Seconds the row spent parked at the review stage waiting on someone "
    "outside the pipeline, including operator decisions, sign-offs and crash "
    "or reap parks: every BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF "
    "segment, whatever the reason. Null when the recorded status transitions "
    "do not cover the window; 0.0 when they do and the row was never parked."
)
WALL_SECONDS = (
    "Seconds since the previous round's record (round 1: since this row's "
    "attributable `s3_review_started` event), so it includes the fix cycle in "
    "between; null when either end is unknown. See `stage_seconds` for the "
    "split; when all four of its parts are recorded they sum to this value."
)
ROUND_STAGE_SECONDS = (
    "Where this round's `wall_seconds` window went, in queued, reviewing, "
    "repairing and gate seconds (round 1 starts at its `s3_review_started` "
    "event, so the wait before the review claim is not in it). Derived at "
    "report time from this row's `task.transition` events and its fix "
    "sessions; a part is null when that evidence is missing (see `notes`), "
    "and an all-null block means nothing was recorded. Time in two states at "
    "once counts once, as gate, then queued, then repairing, then reviewing."
)
SUMMARY_STAGE_SECONDS = (
    "Where the whole review stage's time went, in queued, reviewing, "
    "repairing and gate seconds: every review-stage status segment from entry "
    "to exit, including the initial queue wait before round 1 and any gate "
    "after the last round. Derived at report time; an open wait is counted up "
    "to `stage_seconds_as_of`. A part is null when its evidence is missing "
    "(see `notes`); an all-null block means nothing was recorded."
)
SUMMARY_AS_OF = (
    "ISO time up to which a still-open wait (the row's last status segment, "
    "or a live fix session) was counted into `stage_seconds`; null when "
    "nothing open was counted."
)

#: The sentence appended to the ``cw review history`` event-tie paragraph.
STAGE_TIE_SENTENCE = (
    "A `task.transition` event is used for stage time only when it names this "
    "client and ticket id and was created within this row's lifetime (not "
    "before the row was created, and not at or after the next re-enqueued "
    "row's creation); a session id is not a tie."
)
#: The new paragraph of the ``cw review history`` docstring.
STAGE_DOC_PARAGRAPH = (
    "Stage time: each round, and the summary, also splits where the time went "
    "into `stage_seconds`. `queued` is the row PENDING at the review stage. "
    "`reviewing` is the row RUNNING with no fix session running, so it "
    "includes the gap between a fix handoff and the fix session's start. "
    "`repairing` runs from a fix session's `started_at` to its "
    "`completed_at`. `gate` is the row parked at the review stage waiting on "
    "someone outside the pipeline: operator decisions, sign-offs and crash or "
    "reap parks. All of it is derived from the row's `task.transition` events "
    "and its fix sessions; nothing new is recorded. A part is `not recorded` "
    "(null in `--json`) when its evidence is missing, for example after the "
    "event inbox was pruned, and `0.0` when the row spent no time there. A "
    "still-open wait is counted up to `summary.stage_seconds_as_of`."
)

#: Note 1: no usable ``task.transition`` event.
NOTE_NO_TRANSITIONS = (
    "no task.transition event could be used for this row (the event inbox may "
    "have been pruned); queued, reviewing and gate stage timings not recorded"
)
#: Note 5: an open segment with no current time.
NOTE_OPEN_SEGMENT = (
    "the row's last status segment or a live fix session is still open and no "
    "current time was available; not counted in stage timings"
)
#: Note 11: the summary's head is not covered by the transition events.
NOTE_HEAD_NOT_COVERED = (
    "queued, reviewing and gate stage timings not recorded in the summary: "
    "the row's task.transition events do not reach back to the start of its "
    "review stage (the event inbox may have been partly pruned)"
)


def note_uncovered(rounds: str) -> str:
    """Note 2: the rounds whose window the transition events do not cover."""
    return (
        f"queued, reviewing and gate stage timings not recorded for round(s) "
        f"{rounds}: the row's task.transition events do not cover the round's "
        "window"
    )


def note_unusable(rounds: str) -> str:
    """Note 3: the rounds whose wall-time window is unknown or not positive."""
    return (
        f"stage timings not recorded for round(s) {rounds}: the round's "
        "wall-time window is unknown or not positive"
    )


def note_no_fix_span(rounds: str) -> str:
    """Note 4: the rounds whose fix was dispatched with no fix span."""
    return (
        f"repairing stage timing not recorded for round(s) {rounds}: a fix was "
        "dispatched but no fix session of this row overlaps the round's window"
    )


def note_last_mismatch(event_status: str, row_status: str) -> str:
    """Note 6: the last transition event disagrees with the row's status."""
    return (
        f"the last task.transition event for this row says '{event_status}' "
        f"but the row is '{row_status}'; that segment is not counted in stage "
        "timings"
    )


def note_foreign_transitions(count: int, ticket_id: str = ACME_TICKET) -> str:
    """Note 7: transition events that could not be tied or are unusable."""
    return (
        f"{count} task.transition event(s) for ticket id '{ticket_id}' could "
        "not be tied to this row or carry an unusable payload; not used"
    )


def note_pre_row_fix(count: int) -> str:
    """Note 8: fix sessions that started before the row was created."""
    return (
        f"{count} fix session(s) of this client started before this row was "
        "created; not counted in repairing stage timing"
    )


def note_fix_no_completed_at(count: int) -> str:
    """Note 9: terminal fix sessions with no ``completed_at``."""
    return (
        f"{count} terminal fix session(s) of this row have no completed_at; "
        "not counted in repairing stage timing"
    )


def note_unreadable_inbox(exc_name: str) -> str:
    """Note 10: the event inbox could not be read."""
    return (
        f"event inbox unreadable ({exc_name}); round-1 wall time, voided "
        "findings and queued, reviewing and gate stage timings not shown"
    )


class Step(NamedTuple):
    """One ``task.transition`` of a timing fixture's row."""

    at: str
    status: QueueItemStatus
    stage: Stage = Stage.REVIEW
    blocked_reason: str | None = None


def timing_task(**overrides: object) -> TicketTask:
    """A COMPLETED review row created at :data:`ROW_CREATED`, bound to acme's session.

    Events earlier than the row's ``created_at`` are never its own, so every
    timing fixture pins it before its first transition.
    """
    kwargs: dict[str, object] = {
        "created_at": datetime.fromisoformat(ROW_CREATED),
        "session_id": ACME_SESSION,
    }
    kwargs.update(overrides)
    return review_task(**kwargs)


def transition_event(
    task: TicketTask,
    new_status: QueueItemStatus,
    at: str,
    *,
    stage: Stage = Stage.REVIEW,
    client: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    """Emit the real ``task.transition`` event of a move to *new_status* at *at*.

    Mutates a throwaway copy of *task* (never *task* itself) and calls the real
    ``transition_task_status`` under ``freeze_time(at)``, so the payload is the
    producer's. The copy starts in a status that differs from *new_status*, so
    the seam always emits.
    """
    old = (
        QueueItemStatus.PENDING
        if new_status is QueueItemStatus.RUNNING
        else QueueItemStatus.RUNNING
    )
    row = task.model_copy(
        deep=True,
        update={"status": old, "stage": stage, "client": client or task.client},
    )
    with freeze_time(at):
        transition_task_status(
            row, new_status, blocked_reason=blocked_reason, unproductive=False
        )


def status_chain(task: TicketTask, *steps: Step) -> None:
    """Emit one real ``task.transition`` event per step of *steps*, in order."""
    for step in steps:
        transition_event(
            task,
            step.status,
            step.at,
            stage=step.stage,
            blocked_reason=step.blocked_reason,
        )


def fix_session_for(
    task: TicketTask,
    sid: str,
    started: str,
    completed: str | None = None,
    *,
    worktree: Path,
    status: SessionStatus | None = None,
    **overrides: object,
) -> Session:
    """A FIX session of *task*'s client, homed at *worktree*, started at *started*.

    *status* defaults to COMPLETED with a *completed* time and ACTIVE without.
    """
    chosen = status or (SessionStatus.COMPLETED if completed else SessionStatus.ACTIVE)
    return session_for(
        task,
        sid,
        purpose=SessionPurpose.FIX,
        worktree_path=worktree,
        status=chosen,
        started_at=datetime.fromisoformat(started),
        completed_at=datetime.fromisoformat(completed) if completed else None,
        **overrides,
    )


#: The fix cycle's timeline (all 2026-10-27), shared by the timing, render and CLI
#: tests. Round 1 is recorded at 12:00:00 (window 11:50:00-12:00:00), its fix
#: session runs 12:05:00-12:25:00, round 2 is recorded at 12:50:00.
FIX_CYCLE_STAMPS = ("2026-10-27T12:00:00+00:00", "2026-10-27T12:50:00+00:00")
FIX_SESSION_STARTED = "2026-10-27T12:05:00+00:00"
FIX_SESSION_COMPLETED = "2026-10-27T12:25:00+00:00"
#: PENDING (queued 30m), RUNNING, [fix], PENDING (4m30s), RUNNING, round 2, gate
#: BLOCKED_ON_USER 1h02m, RUNNING, COMPLETED.
FIX_CYCLE_STEPS = (
    Step("2026-10-27T11:00:00+00:00", QueueItemStatus.PENDING),
    Step("2026-10-27T11:30:00+00:00", QueueItemStatus.RUNNING),
    Step("2026-10-27T12:25:30+00:00", QueueItemStatus.PENDING),
    Step("2026-10-27T12:30:00+00:00", QueueItemStatus.RUNNING),
    Step(
        "2026-10-27T13:00:00+00:00",
        QueueItemStatus.BLOCKED_ON_USER,
        blocked_reason="review_pending_approval",
    ),
    Step("2026-10-27T14:02:00+00:00", QueueItemStatus.RUNNING),
    Step("2026-10-27T14:10:00+00:00", QueueItemStatus.COMPLETED),
)


def fix_cycle_ledger() -> ConvergenceLedger:
    """A dirty round (two fixes dispatched) then a clean one, through the engine."""
    ledger = fresh_ledger(ticket_id=ACME_TICKET)
    ledger, _ = record(
        ledger,
        round_finding(summary="a"),
        round_finding(summary="b"),
        sha="sha-0001",
    )
    ledger, _ = record(ledger, sha="sha-0002")
    assert ledger.status == "converged"
    assert ledger.rounds[0].new_fix == 2
    return timed(ledger, *FIX_CYCLE_STAMPS)


def seed_fix_cycle(
    tmp_path: Path,
    steps: Sequence[Step] = FIX_CYCLE_STEPS,
    *,
    ledger: ConvergenceLedger | None = None,
    with_start: bool = True,
) -> tuple[TicketTask, list[Session]]:
    """Write the fix cycle's ledger, ``stage.entered`` start and transition chain.

    Returns the row (created before the chain, COMPLETED) and its one FIX
    session (home: the row's derived worktree). The review start is 11:50:00;
    *with_start* ``False`` omits that event, *ledger* replaces the ledger.
    """
    worktree = derived_worktree(tmp_path)
    write_ledger(worktree, ledger or fix_cycle_ledger())
    task = timing_task()
    if with_start:
        stage_entered_event(ACME_TICKET, ACME_SESSION, "2026-10-27T11:50:00+00:00")
    status_chain(task, *steps)
    fix = fix_session_for(
        task,
        "sess-fix",
        FIX_SESSION_STARTED,
        FIX_SESSION_COMPLETED,
        worktree=worktree,
    )
    return task, [fix]
