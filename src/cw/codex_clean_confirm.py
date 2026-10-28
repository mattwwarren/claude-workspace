"""Converge-to-clean gate for the codex REVIEW lane (#2791).

Under ``review.convergence: clean`` the finalize ``verify-tip`` check needs a
converged ledger at ``<worktree>/.cw/review-convergence.json``. The codex lane
keeps its own fix loop (``run_review_with_fix_loop``), so this gate is what
writes that ledger: the detached ``cw codex run`` driver records the clean
codex loop round, then runs the independent Claude confirm round
(:func:`cw.claude_review.run_claude_review`) and records it too.

Two entry points, both called from
``cw.codex_background._review_and_confirm``:

- :func:`resume_pending_confirm` runs BEFORE the codex loop. A ledger already
  ``awaiting_confirm`` at HEAD (a requeue after a Claude outage) skips the
  loop and goes straight to the confirm. Every other ledger state returns
  ``None`` and the loop runs.
- :func:`apply_clean_confirm` runs AFTER the loop (and after the unpushed-HEAD
  guard). It returns the result unchanged for a blocking-mode client, a
  blocked result or a missing verdict, so blocking mode never reads or writes
  a ledger.

The loop backend ("ran on") is the constant :data:`cw.models.CODEX_BACKEND`:
nothing else reaches this module. The confirm backend is
``resolve_confirm_backend(client.review.confirm_backend, CODEX_BACKEND)``:
unset or ``claude`` runs Claude, explicit ``codex`` is not independent and
``none`` opts out (both converge on the clean loop round alone).

Failure handling, all typed:

- Claude unreachable (binary missing, every role a backend error, or the call
  raised): ``record_confirm_failure`` and the ledger's
  ``confirm_backend_unavailable`` blocker (an ``agent_block`` once escalated).
- A round that could not complete on a healthy backend (partial,
  unparseable, discarded findings, timeout, an exhausted wall-clock budget,
  a worktree the reviewer mutated): the synthesized ``codex_*`` result,
  unchanged, with nothing recorded, so the ledger stays ``awaiting_confirm``.
- Any ledger, git or file step that fails after a completed codex review:
  ``codex_convergence_ledger_error`` (retry-eligible), keeping the codex review
  and verdict. These never reach the review thread's PENDING-revert handler.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_output
from cw.auto_dev_result import AutoDevResult, Blocker
from cw.auto_dev_result.schema._vocab import REVIEW_NOT_CONVERGING_BLOCKER_REASON
from cw.claude_review import (
    CLAUDE_BACKEND_DETAILS_PREFIX,
    CLAUDE_REVIEWER_LABEL,
    _which_claude,
    run_claude_review,
)
from cw.codex_review import (
    CODEX_CONVERGENCE_LEDGER_ERROR,
    CODEX_MUST_FIX_FINDINGS,
    make_codex_blocked,
)
from cw.dispatch_state import ExecutorBlockedMarker, save_executor_blocked_marker
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_STOPPED,
    confirm_blocker_fragment,
    friction_lines,
    from_verdict,
    frozen_counts_from_verdict,
    history_rewritten,
    load_ledger,
    new_ledger,
    reconcile_ledger_for_init,
    record_confirm_failure,
    record_round,
    resolve_confirm_backend,
    save_ledger,
    save_round_verdict,
    sentinel_review_fragment,
    stop_summary,
    unread_from_verdict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.claude_review import ClaudeReviewRound, ClaudeRunner
    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger, RoundReport
    from cw.review_findings import AcceptedFinding, ReviewVerdict

_log = logging.getLogger(__name__)

#: Step 4b comment title and artifact-header label for a codex-built result.
CODEX_REVIEWER_LABEL = "Codex"

#: Every ledger, git or file failure the gate turns into a typed park.
#: ``ConvergenceError`` is a ``CwError``; a pydantic ``ValidationError`` (a
#: corrupt ledger) is a ``ValueError``.
_BOOKKEEPING_ERRORS = (
    OSError,
    CwError,
    ValueError,
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
)

#: The convergence keys merged into ``result.review``. The rest of
#: :func:`sentinel_review_fragment` (``fix_cycles_used``, the frozen counts)
#: would overwrite the codex lane's own figures, so it is never merged.
_CONVERGENCE_KEYS = (
    "convergence_mode",
    "rounds_run",
    "converged",
    "final_round_clean_sha",
    "confirm_backend",
)

#: Rationale stamped on a confirm round's SHOULD_FIX findings: like the codex
#: lane's own loop, SHOULD_FIX never gates, so the ledger settles it as defer.
_CONFIRM_DEFER_RATIONALE = (
    "SHOULD_FIX from the Claude confirm round; non-gating on the codex lane"
)
_DETAIL_LIMIT = 300


class CleanConfirmOutcome(NamedTuple):
    """The gate's answer: the result to persist, its verdict, and its label.

    ``reviewer_label`` titles the Step 4b comment and the unparseable
    artifact, so a Claude-built result is never posted under a Codex header.
    """

    result: AutoDevResult
    verdict: ReviewVerdict | None
    reviewer_label: str


@dataclass
class _Gate:
    """One gate run: its identity, the Claude seam, and what it has touched."""

    task: TicketTask
    worktree: Path
    client: ClientConfig
    sid: str
    runner: ClaudeRunner
    deadline: float | None
    ledger: ConvergenceLedger | None = None
    tokens: list[str] = field(default_factory=list)

    @property
    def ledger_path(self) -> Path:
        return self.worktree / LEDGER_RELATIVE_PATH


def _now() -> str:
    """ISO-8601 UTC, the format ``cw review converge`` stamps."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _DETAIL_LIMIT:
        return collapsed
    return collapsed[:_DETAIL_LIMIT] + "..."


def _head(worktree: Path) -> str:
    return git_output(["rev-parse", "HEAD"], cwd=worktree).strip()


def _fork_point(gate: _Gate) -> str:
    """``git merge-base origin/<default> HEAD``, the prose's derivation."""
    return git_output(
        ["merge-base", f"origin/{gate.client.default_branch}", "HEAD"],
        cwd=gate.worktree,
    ).strip()


def _is_clean_client(client: ClientConfig) -> bool:
    return client.review.convergence == "clean"


def _load_reconciled_ledger(gate: _Gate, head: str) -> ConvergenceLedger:
    """The existing ledger reconciled exactly as ``converge init`` would."""
    existing = load_ledger(gate.ledger_path)
    fork_point = _fork_point(gate)
    client = gate.client
    max_cycles = client.review.effective_max_cycles
    fresh = new_ledger(
        ticket_id=gate.task.ticket_id,
        fork_point=fork_point,
        checkpoint_3a_sha=head,
        max_cycles=max_cycles,
        loop_backend=CODEX_BACKEND,
        confirm=resolve_confirm_backend(client.review.confirm_backend, CODEX_BACKEND),
    )
    owned = (
        existing is not None
        and existing.ticket_id == gate.task.ticket_id
        and existing.fork_point == fork_point
    )
    reconciled = reconcile_ledger_for_init(
        existing,
        fresh=fresh,
        ticket_id=gate.task.ticket_id,
        fork_point=fork_point,
        history_rewritten=(
            history_rewritten(existing, gate.worktree, head) if owned else False
        ),
        head_sha=head,
        max_cycles=max_cycles,
        now=_now(),
    )
    if reconciled.reset_reason is not None:
        _log.info(
            "clean-mode ledger reset ticket=%s client=%s reason=%s",
            gate.task.ticket_id,
            client.name,
            reconciled.reset_reason,
        )
        gate.tokens.append(f"convergence_ledger_reset: {reconciled.reset_reason}")
    ledger = reconciled.ledger
    gate.ledger = ledger
    if ledger.loop_backend != CODEX_BACKEND:
        msg = (
            f"foreign convergence ledger: {LEDGER_RELATIVE_PATH} was written by "
            f"loop backend {ledger.loop_backend!r}, not {CODEX_BACKEND!r}; move "
            "it aside, then requeue the ticket at the review stage "
            f"(cw dev-queue requeue {gate.task.ticket_id} -c {client.name} "
            "--stage review)"
        )
        raise CwError(msg)
    return ledger


def _record_loop_round(
    gate: _Gate, ledger: ConvergenceLedger, verdict: ReviewVerdict
) -> tuple[ConvergenceLedger, RoundReport]:
    """Record the clean codex loop round and save it before the confirm runs.

    Saving here is what makes the requeue-resume real: a crash or an outage
    during the confirm leaves ``awaiting_confirm`` on disk.
    """
    ledger, report = record_round(
        ledger,
        kind="loop",
        backend=CODEX_BACKEND,
        reviewed_sha=verdict.reviewed_sha,
        findings=[],
        roster=[record.reviewer_role for record in verdict.agents_run],
        now=_now(),
        unread=unread_from_verdict(verdict),
        frozen=frozen_counts_from_verdict(verdict),
    )
    save_ledger(gate.ledger_path, ledger)
    gate.ledger = ledger
    return ledger, report


def _finish(gate: _Gate, result: AutoDevResult, *, park: bool) -> AutoDevResult:
    """Merge the convergence fields and the friction, then re-validate.

    A *park* never reports ``converged`` (the ledger may be converged while
    the result still parks on a bookkeeping failure).
    """
    ledger = gate.ledger
    update: dict[str, object]
    if ledger is None:
        resolved = resolve_confirm_backend(
            gate.client.review.confirm_backend, CODEX_BACKEND
        )
        update = {
            "convergence_mode": "clean",
            "converged": False,
            "confirm_backend": resolved.name,
        }
    else:
        fragment = sentinel_review_fragment(ledger)
        update = {key: fragment[key] for key in _CONVERGENCE_KEYS}
        update["rounds_run"] = len(ledger.rounds) or None
    if park:
        update["converged"] = False
        update["final_round_clean_sha"] = None
    friction = [
        *result.friction_highlights,
        *(friction_lines(ledger) if ledger is not None else []),
        *gate.tokens,
    ]
    merged = result.model_copy(
        update={
            "review": result.review.model_copy(update=update),
            "friction_highlights": friction,
        }
    )
    return AutoDevResult.model_validate(merged.model_dump(mode="json"))


def _parked(
    gate: _Gate, base: CleanConfirmOutcome | None, blocker: Blocker
) -> CleanConfirmOutcome:
    """A park carrying *blocker*, keeping *base*'s review and verdict.

    *base* is the completed codex review on the loop path, and ``None`` on the
    pre-loop resume path before any review completed in this run.
    """
    blocked = make_codex_blocked(
        ticket_id=gate.task.ticket_id,
        worktree=gate.worktree,
        reason=blocker.reason,
        details=blocker.details,
        retry_eligible=blocker.retry_eligible,
    )
    update: dict[str, object] = {"blocker": blocker}
    if base is not None:
        update["review"] = base.result.review
    result = _finish(gate, blocked.model_copy(update=update), park=True)
    if base is None:
        return CleanConfirmOutcome(result, None, CLAUDE_REVIEWER_LABEL)
    return CleanConfirmOutcome(result, base.verdict, base.reviewer_label)


def _ledger_error_outcome(
    gate: _Gate, base: CleanConfirmOutcome | None, exc: Exception
) -> CleanConfirmOutcome:
    _log.exception(
        "convergence bookkeeping failed after a completed review ticket=%s "
        "client=%s session=%s",
        gate.task.ticket_id,
        gate.client.name,
        gate.sid,
    )
    gate.tokens.append(f"convergence_bookkeeping_failed: {type(exc).__name__}")
    blocker = Blocker(
        stage="stage3_review",
        reason=CODEX_CONVERGENCE_LEDGER_ERROR,
        details=f"{type(exc).__name__}: {_one_line(str(exc))}",
        retry_eligible=True,
    )
    return _parked(gate, base, blocker)


def _converged_outcome(gate: _Gate, base: CleanConfirmOutcome) -> CleanConfirmOutcome:
    ledger = gate.ledger
    _log.info(
        "clean-mode converged ticket=%s client=%s rounds_run=%s "
        "confirm_backend=%s sha=%s",
        gate.task.ticket_id,
        gate.client.name,
        len(ledger.rounds) if ledger is not None else 0,
        ledger.confirm_backend if ledger is not None else None,
        ledger.last_clean_sha if ledger is not None else None,
    )
    result = _finish(gate, base.result, park=False)
    return CleanConfirmOutcome(result, base.verdict, base.reviewer_label)


def _stopped_outcome(
    gate: _Gate, ledger: ConvergenceLedger, base: CleanConfirmOutcome
) -> CleanConfirmOutcome:
    """A terminal stop at this tip: park ``review_not_converging``, record nothing."""
    summary = stop_summary(ledger) or {}
    reason = summary.get("reason") or ledger.stop_reason
    blocker = Blocker(
        stage="stage3_review",
        reason=REVIEW_NOT_CONVERGING_BLOCKER_REASON,
        details=(
            f"convergence ledger stopped ({reason}); release: "
            f"{summary.get('release_hint', '')}"
        ),
        retry_eligible=False,
    )
    return _parked(gate, base, blocker)


def _unavailable_outcome(
    gate: _Gate,
    ledger: ConvergenceLedger,
    base: CleanConfirmOutcome | None,
    *,
    cause: str,
    detail: str,
) -> CleanConfirmOutcome:
    """Claude unreachable: count the outage and park on the ledger's blocker."""
    ledger, report = record_confirm_failure(
        ledger, cause=cause, detail=_one_line(detail), now=_now()
    )
    save_ledger(gate.ledger_path, ledger)
    gate.ledger = ledger
    _log.warning(
        "confirm backend unavailable ticket=%s client=%s session=%s cause=%s "
        "decision=%s",
        gate.task.ticket_id,
        gate.client.name,
        gate.sid,
        cause,
        report.decision,
    )
    gate.tokens.append(f"clean_confirm_backend_unavailable: cause={cause}")
    fragment = confirm_blocker_fragment(
        ledger,
        escalated=report.decision == "stop_confirm_escalated",
        client=gate.client.name,
    )
    fragment["details"] = f"{CLAUDE_BACKEND_DETAILS_PREFIX}{fragment['details']}"
    return _parked(gate, base, Blocker.model_validate(fragment))


def _decided(item: AcceptedFinding) -> AcceptedFinding:
    """*item* with a disposition a confirm round can record.

    MUST_FIX is a ``fix`` (a ``dropped``/``unresolved`` one too: nobody fixed
    it); SHOULD_FIX not already rejected or deferred is ``deferred``.
    """
    severity = item.finding.severity
    if severity == "SHOULD_FIX" and item.disposition not in ("rejected", "deferred"):
        return item.model_copy(
            update={
                "disposition": "deferred",
                "disposition_detail": _CONFIRM_DEFER_RATIONALE,
            }
        )
    if severity == "MUST_FIX" and item.disposition in ("dropped", "unresolved"):
        return item.model_copy(update={"disposition": "fixed"})
    return item


def _record_confirm_round(
    gate: _Gate,
    ledger: ConvergenceLedger,
    base: CleanConfirmOutcome | None,
    round_: ClaudeReviewRound,
    claude_verdict: ReviewVerdict,
) -> CleanConfirmOutcome:
    """Record the Claude round, persist its verdict, then converge or park.

    The verdict file is written first (the ledger names the round), then the
    ledger. A clean round converges on *base* (the codex result on the loop
    path, the Claude result on the resume path). A dirty one parks with the
    Claude ``codex_must_fix_findings`` result; a ``stop_*`` decision appends
    the ledger's release hint to its details.
    """
    decided = claude_verdict.model_copy(
        update={"accepted": [_decided(item) for item in claude_verdict.accepted]}
    )
    findings, unread = from_verdict(decided)
    ledger, report = record_round(
        ledger,
        kind="confirm",
        backend=ledger.confirm_backend,
        reviewed_sha=claude_verdict.reviewed_sha,
        findings=findings,
        roster=[record.reviewer_role for record in claude_verdict.agents_run],
        now=_now(),
        unread=unread,
    )
    save_round_verdict(gate.ledger_path, report.round, claude_verdict)
    save_ledger(gate.ledger_path, ledger)
    gate.ledger = ledger
    claude = CleanConfirmOutcome(round_.result, claude_verdict, CLAUDE_REVIEWER_LABEL)
    if report.decision == "clean":
        return _converged_outcome(gate, base or claude)
    result = round_.result
    blocker = result.blocker
    if blocker is not None and report.decision.startswith("stop_"):
        summary = stop_summary(ledger) or {}
        hinted = blocker.model_copy(
            update={
                "details": (
                    f"{blocker.details}\n\nconvergence ledger stopped "
                    f"({report.reason}); release: {summary.get('release_hint', '')}"
                )
            }
        )
        result = result.model_copy(update={"blocker": hinted})
    return CleanConfirmOutcome(
        _finish(gate, result, park=True), claude_verdict, CLAUDE_REVIEWER_LABEL
    )


def _remaining_budget(gate: _Gate) -> int | None:
    """Seconds left of the thread's single deadline (``None``: no deadline)."""
    if gate.deadline is None:
        return None
    remaining = int(gate.deadline - time.monotonic())
    if remaining <= 0:
        _log.warning(
            "confirm skipped: wall-clock budget exhausted ticket=%s client=%s "
            "session=%s",
            gate.task.ticket_id,
            gate.client.name,
            gate.sid,
        )
        return 0
    return remaining


def _healthy_failure_outcome(
    gate: _Gate, round_: ClaudeReviewRound
) -> CleanConfirmOutcome:
    """The round could not complete on a healthy backend: return it unchanged.

    Nothing is recorded and no outage is counted, so the ledger stays
    ``awaiting_confirm`` and a requeue resumes at the confirm. Covers a
    partial or unparseable round, discarded findings, a timeout, an exhausted
    budget and a reviewer that mutated the worktree (G4).
    """
    blocker = round_.result.blocker
    _log.warning(
        "confirm round failed on a healthy backend ticket=%s client=%s "
        "session=%s reason=%s",
        gate.task.ticket_id,
        gate.client.name,
        gate.sid,
        blocker.reason if blocker is not None else None,
    )
    return CleanConfirmOutcome(round_.result, round_.verdict, CLAUDE_REVIEWER_LABEL)


def _run_confirm(
    gate: _Gate, ledger: ConvergenceLedger, base: CleanConfirmOutcome | None
) -> CleanConfirmOutcome:
    """Run the Claude confirm on an ``awaiting_confirm`` ledger and record it."""
    client = gate.client
    _log.info(
        "clean-mode confirm starting ticket=%s client=%s session=%s "
        "confirm_backend=%s sha=%s",
        gate.task.ticket_id,
        client.name,
        gate.sid,
        ledger.confirm_backend,
        ledger.awaiting_confirm_sha,
    )
    save_executor_blocked_marker(
        ExecutorBlockedMarker(
            client=client.name,
            ticket_id=gate.task.ticket_id,
            executor="claude",
            reviewer_role="confirm",
            started_at=datetime.now(UTC),
            session_id=gate.sid,
        )
    )
    if _which_claude() is None:
        return _unavailable_outcome(
            gate,
            ledger,
            base,
            cause="binary_missing",
            detail="claude executable not found on PATH",
        )
    try:
        round_ = run_claude_review(
            runner=gate.runner,
            task=gate.task,
            worktree=gate.worktree,
            default_branch=client.default_branch,
            base_ref=ledger.fork_point,
            model=client.worker_model,
            wall_clock_budget_seconds=_remaining_budget(gate),
            session_id=f"confirm-{gate.sid}",
        )
    except (OSError, CwError) as exc:
        return _unavailable_outcome(
            gate, ledger, base, cause="crash", detail=f"{type(exc).__name__}: {exc}"
        )
    blocker = round_.result.blocker
    if round_.backend_down:
        detail = blocker.details if blocker is not None else "no reviewer document"
        return _unavailable_outcome(
            gate, ledger, base, cause="backend_error", detail=detail
        )
    if round_.verdict is None or (
        blocker is not None and blocker.reason != CODEX_MUST_FIX_FINDINGS
    ):
        return _healthy_failure_outcome(gate, round_)
    return _record_confirm_round(gate, ledger, base, round_, round_.verdict)


def _gate_after_loop(
    gate: _Gate, base: CleanConfirmOutcome, verdict: ReviewVerdict
) -> CleanConfirmOutcome:
    """The ledger-state table (plan step 5), after a clean codex loop."""
    head = _head(gate.worktree)
    if verdict.reviewed_sha != head:
        msg = (
            f"the codex verdict reviewed {verdict.reviewed_sha} but HEAD is {head}; "
            "a converged ledger must name the sha that was reviewed"
        )
        raise CwError(msg)
    ledger = _load_reconciled_ledger(gate, head)
    if ledger.status == LEDGER_STATUS_CONVERGED:
        # reconcile_ledger_for_init demotes a converged ledger whose HEAD
        # moved, so this is the idempotent last_clean_sha == HEAD row.
        return _converged_outcome(gate, base)
    if ledger.status == LEDGER_STATUS_STOPPED:
        return _stopped_outcome(gate, ledger, base)
    if (
        ledger.status != LEDGER_STATUS_AWAITING_CONFIRM
        or ledger.awaiting_confirm_sha != head
    ):
        ledger, report = _record_loop_round(gate, ledger, verdict)
        if report.decision == "clean":
            return _converged_outcome(gate, base)
    return _run_confirm(gate, ledger, base)


def apply_clean_confirm(
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    sid: str,
    runner: ClaudeRunner,
    deadline: float | None,
    result: AutoDevResult,
    verdict: ReviewVerdict | None,
) -> CleanConfirmOutcome:
    """Record the codex loop round and run the confirm (clean-mode clients only).

    Returns *result* and *verdict* unchanged, labeled Codex, for a
    blocking-mode client, a blocked *result* or a missing *verdict*: no ledger
    is read or written on those paths. *deadline* is the review thread's
    ``time.monotonic()`` deadline (``None``: no wall-clock budget).
    """
    base = CleanConfirmOutcome(result, verdict, CODEX_REVIEWER_LABEL)
    if not _is_clean_client(client) or result.blocker is not None or verdict is None:
        return base
    gate = _Gate(task, worktree, client, sid, runner, deadline)
    try:
        return _gate_after_loop(gate, base, verdict)
    except _BOOKKEEPING_ERRORS as exc:
        return _ledger_error_outcome(gate, base, exc)


def _resumable(
    ledger: ConvergenceLedger | None, task: TicketTask, head: str, fork_point: str
) -> bool:
    return (
        ledger is not None
        and ledger.ticket_id == task.ticket_id
        and ledger.fork_point == fork_point
        and ledger.loop_backend == CODEX_BACKEND
        and ledger.status == LEDGER_STATUS_AWAITING_CONFIRM
        and ledger.awaiting_confirm_sha == head
    )


def resume_pending_confirm(
    *,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    sid: str,
    runner: ClaudeRunner,
    deadline: float | None,
) -> CleanConfirmOutcome | None:
    """Run only the confirm when the ledger already awaits one at HEAD.

    ``None`` (run the codex loop) for a blocking-mode client, a missing or
    unreadable ledger, another ticket or fork point, another status, or an
    ``awaiting_confirm`` sha that is not HEAD. After a dirty Claude confirm the
    ledger is no longer ``awaiting_confirm``, so a requeue re-runs the loop.
    """
    if not _is_clean_client(client):
        return None
    gate = _Gate(task, worktree, client, sid, runner, deadline)
    try:
        head = _head(worktree)
        ledger = load_ledger(gate.ledger_path)
        fork_point = _fork_point(gate)
    except _BOOKKEEPING_ERRORS:
        _log.warning(
            "clean-mode confirm resume: ledger state unreadable, running the "
            "codex loop ticket=%s client=%s",
            task.ticket_id,
            client.name,
            exc_info=True,
        )
        return None
    if ledger is None or not _resumable(ledger, task, head, fork_point):
        return None
    _log.info(
        "clean-mode confirm resume: skipping codex fix loop ticket=%s client=%s "
        "head=%s",
        task.ticket_id,
        client.name,
        head,
    )
    gate.ledger = ledger
    gate.tokens.append("clean_confirm_resumed")
    try:
        return _run_confirm(gate, ledger, None)
    except _BOOKKEEPING_ERRORS as exc:
        return _ledger_error_outcome(gate, None, exc)
