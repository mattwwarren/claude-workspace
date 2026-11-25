"""The codex PLAN resumed-round contract, run by cw (#3464, RFC 0018 C1).

cw, not the read-only model, owns every mechanical rule of a resumed round:
the resolutions delta (and its one-shot revision), the approved-fingerprint
fast path, the round counter and its cap, and the fingerprint settle. The
round's bookkeeping lives in one :class:`RoundBook`, which every draft write
renders, so the leading block is always in grammar order.

Revocation needs positive evidence (R8): a new row source, or a different
marker source while the applied comment or body source is itself still
observed. An applied comment or body source the snapshot merely does not
show (deleted, unbound, partial or unavailable snapshot) never revokes: the
round parks for the operator, blocked ``agent_block`` and retry-eligible,
with the approval standing and the draft untouched.

The codex fast path is fingerprint equality only; the operator-authority
alternate stays #3465, so there is no third condition to compose with
(``fails-closed``). cw still persists the ``body_sha`` component.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from cw.auto_dev_result import AutoDevResult, Health
from cw.auto_dev_result.schema._vocab import AGENT_BLOCK_BLOCKER_REASON
from cw.codex_plan._exits import _Exit, _with_friction
from cw.codex_plan._result import (
    PLAN_AMBIGUITIES_PENDING,
    PLAN_PREMISES_PENDING,
    STAGE_PLAN_GENERATED,
    draft_fingerprint_on_disk,
    stamp_plan_result,
    write_plan_draft,
)
from cw.codex_plan._revise import revise_plan
from cw.codex_plan._settle import park_state
from cw.codex_plan._tracker import (
    BODY_SOURCE_PREFIX,
    COMMENT_SOURCE_PREFIX,
    ROW_SOURCE_PREFIX,
    SOURCE_NONE,
    body_resolutions_section,
    discover_resolutions_source,
    observe_tracker,
    source_observed,
)
from cw.codex_review import CODEX_MODEL_CAPACITY
from cw.dev_queue.approval import revoke_plan_approval
from cw.exceptions import CwError
from cw.plan_files import (
    parse_plan_files_modified,
    parse_plan_lines_estimate,
    parse_plan_scope_tier,
)
from cw.plan_fingerprint import (
    ATTEMPT_FAILED,
    ATTEMPT_STARTED,
    ATTEMPT_SUCCEEDED,
    ApprovalRevoked,
    LeadingBlock,
    ResolutionsAttempt,
    compute_plan_draft_fingerprint,
    is_plan_draft_fingerprint,
)
from cw.stage_verification.plan_gates import PLAN_STAGE_COMPLETE, has_approval_evidence

if TYPE_CHECKING:
    from cw.codex_plan._result import ResumeDraft
    from cw.codex_plan._run import _McpTarget, _Run
    from cw.codex_plan._settle import ParkState
    from cw.codex_plan._tracker import ResolutionsSource, TrackerObservation
    from cw.models import TicketTask
    from cw.ticket_snapshot import SnapshotContext, TicketSnapshot

#: The round cap: a park at N >= ROUND_CAP blocks instead of re-parking.
ROUND_CAP: Final = 2
#: How long a ``started`` attempt reserves its source identity.
ATTEMPT_LEASE: Final = timedelta(minutes=15)
#: The ``reason`` of the fast path's ``s1_ambiguity_scan_skipped`` event.
APPROVED_FINGERPRINT_MATCH: Final = "approved_fingerprint_match"
NO_APPROVAL_EVIDENCE: Final = "no_approval_evidence"
APPROVED_FINGERPRINT_MISMATCH: Final = "approved_fingerprint_mismatch"
NO_SCOPE_TIER: Final = "no_scope_tier"
#: The revoke's audit labels (R13): the codex source, and the delta reason.
REVOKE_SOURCE: Final = "codex_plan_resume"
REVOKE_REASON: Final = "preflight_resolutions_delta"
UNOBSERVED_FRICTION_PREFIX: Final = "resolutions-source-unobserved: "
_UTC_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
#: The Step 4c EXIT park statuses: the round counter counts only these.
EXIT_STATUSES: Final = frozenset({PLAN_AMBIGUITIES_PENDING, PLAN_PREMISES_PENDING})


@dataclass
class RoundBook:
    """The round's mutable bookkeeping state; every draft write renders it."""

    block: LeadingBlock = dataclasses.field(default_factory=LeadingBlock)

    def update(self, **changes: Any) -> None:
        self.block = dataclasses.replace(self.block, **changes)


@dataclass(frozen=True)
class TrackerState:
    """What the round read from the tracker and the row."""

    observation: TrackerObservation
    source: ResolutionsSource
    park: ParkState


def tracker_state(
    snapshot: TicketSnapshot | None, ctx: SnapshotContext, task: TicketTask
) -> TrackerState:
    observation = observe_tracker(snapshot, ctx)
    return TrackerState(
        observation=observation,
        source=discover_resolutions_source(task.operator_resolutions, observation),
        park=park_state(snapshot),
    )


@dataclass(frozen=True)
class GateOutcome:
    """The round after the gate: its run, the draft body, and what follows."""

    run: _Run
    body: str
    reason: str
    row_delta: bool = False

    @property
    def fast_path(self) -> bool:
        return self.reason == APPROVED_FINGERPRINT_MATCH


def _now() -> str:
    return datetime.now(UTC).strftime(_UTC_FORMAT)


def _is_channel_source(source: str | None) -> bool:
    return source is not None and source.startswith(
        (COMMENT_SOURCE_PREFIX, BODY_SOURCE_PREFIX)
    )


def fast_path_reason(task: TicketTask, body: str) -> str:
    """Why *body* does or does not take the fast path (fingerprint only)."""
    if not has_approval_evidence(task):
        return NO_APPROVAL_EVIDENCE
    if task.plan_approved_fingerprint != compute_plan_draft_fingerprint(body):
        return APPROVED_FINGERPRINT_MISMATCH
    if parse_plan_scope_tier(body) is None:
        return NO_SCOPE_TIER
    return APPROVED_FINGERPRINT_MATCH


def synthesize_fast_path_result(run: _Run, body: str) -> AutoDevResult:
    """The fast path's sentinel, from the approved draft itself (no turn ran)."""
    health = Health(
        lowest_agent_confidence=None,
        any_incomplete_risk=False,
        recommendation="PROCEED",
    )
    payload: dict[str, Any] = {
        "status": PLAN_STAGE_COMPLETE,
        "plan_source": "generated",
        "scope": {
            "tier": parse_plan_scope_tier(body),
            "files": len(parse_plan_files_modified(body)),
            "lines_estimate": parse_plan_lines_estimate(body),
            "forbidden_touched": False,
        },
        "health": health.model_dump(mode="json"),
        "friction_highlights": [],
        "ambiguities": [],
        "premises": [],
        "blocker": None,
        "prior_pr_warnings": [],
        "next_actions": [],
    }
    return stamp_plan_result(
        payload, ticket_id=run.task.ticket_id, worktree=run.worktree
    )


def _checkpoint(run: _Run, body: str) -> _Exit | None:
    """Write the draft with the book; a fail-closed block when it cannot."""
    try:
        write_plan_draft(run.worktree, body, book=run.book.block)
    except OSError as exc:
        return run.blocked(
            STAGE_PLAN_GENERATED,
            AGENT_BLOCK_BLOCKER_REASON,
            f"the resolutions bookkeeping could not be written ({type(exc).__name__})",
        )
    return None


def _revoke_approval(run: _Run) -> _Run | _Exit:
    """Revoke the row approval durably (fail closed); the run with a cleared task."""
    try:
        outcome = revoke_plan_approval(
            run.task.ticket_id,
            client_name=run.task.client,
            resolutions_source=REVOKE_SOURCE,
            reason=REVOKE_REASON,
            bounded=True,
        )
    except (CwError, OSError) as exc:
        return run.blocked(
            STAGE_PLAN_GENERATED,
            AGENT_BLOCK_BLOCKER_REASON,
            f"the plan approval could not be revoked ({type(exc).__name__}); "
            "the resolutions delta was not applied",
        )
    previous = outcome.get("previous_fingerprint")
    cleared_any = bool(outcome.get("cleared"))
    if (
        cleared_any
        and isinstance(previous, str)
        and is_plan_draft_fingerprint(previous)
    ):
        run.book.update(revoked=ApprovalRevoked(at=_now(), fingerprint=previous))
    cleared = run.task.model_copy(
        update={"plan_approved_at": None, "plan_approved_fingerprint": None}
    )
    return dataclasses.replace(run, task=cleared)


def _lease_active(attempt: ResolutionsAttempt) -> bool:
    """A lease still in the future; an unparsable one counts as expired."""
    try:
        until = datetime.strptime(attempt.lease_until or "", _UTC_FORMAT).replace(
            tzinfo=UTC
        )
    except ValueError:
        return False
    return until > datetime.now(UTC)


def _reserve(run: _Run, source: str, body: str) -> _Exit | None:
    """Take the 15-minute lease for *source*, or block on a live or used one."""
    attempt = run.book.block.attempted
    if attempt is not None and attempt.source == source:
        if attempt.outcome != ATTEMPT_STARTED:
            return run.blocked(
                STAGE_PLAN_GENERATED,
                AGENT_BLOCK_BLOCKER_REASON,
                f"the one resolutions revision for {source} was already used "
                f"(outcome={attempt.outcome}); record a new resolution to retry",
            )
        if _lease_active(attempt):
            return run.blocked(
                STAGE_PLAN_GENERATED,
                AGENT_BLOCK_BLOCKER_REASON,
                f"an active reservation holds {source} until {attempt.lease_until}",
            )
    lease_until = (datetime.now(UTC) + ATTEMPT_LEASE).strftime(_UTC_FORMAT)
    run.book.update(
        attempted=ResolutionsAttempt(
            source=source, outcome=ATTEMPT_STARTED, lease_until=lease_until
        )
    )
    return _checkpoint(run, body)


def _resolutions_text(run: _Run, state: TrackerState, source: str) -> str:
    """The resolutions the revision applies, verbatim from their channel."""
    if source.startswith(ROW_SOURCE_PREFIX):
        ordered = sorted(run.task.operator_resolutions, key=lambda r: r.round)
        return "\n".join(f"- Round {r.round}: {r.text}" for r in ordered)
    if source.startswith(BODY_SOURCE_PREFIX):
        return body_resolutions_section(state.observation.body) or ""
    comment_id = source.removeprefix(COMMENT_SOURCE_PREFIX)
    bodies = [
        str(c.get("body", ""))
        for c in state.observation.comments
        if str(c["id"]) == comment_id
    ]
    return bodies[-1] if bodies else ""


def _revise_for_resolutions(
    run: _Run, target: _McpTarget, body: str, state: TrackerState, source: str
) -> str | _Exit:
    """The lease, then the one revision turn; the revised body or an exit."""
    prior = run.book.block.attempted
    reserved = _reserve(run, source, body)
    if reserved is not None:
        return reserved
    outcome = revise_plan(
        run, target, body=body, resolutions=_resolutions_text(run, state, source)
    )
    if outcome.text is not None:
        return outcome.text
    if outcome.capacity or outcome.exit is not None:
        run.book.update(attempted=prior)
        released = _checkpoint(run, body)
        if released is not None:
            return released
        return outcome.exit or run.blocked(
            STAGE_PLAN_GENERATED,
            CODEX_MODEL_CAPACITY,
            "the resolutions revision hit a model capacity limit; the "
            "reservation was released",
            retry_eligible=True,
        )
    return _failed_revision(run, body, source, outcome.reason)


def _failed_revision(run: _Run, body: str, source: str, why: str) -> _Exit:
    """A failed revision consumes the attempt, revokes, and blocks."""
    revoked = _revoke_approval(run)
    if isinstance(revoked, _Exit):
        return revoked
    revoked.book.update(attempted=ResolutionsAttempt(source, ATTEMPT_FAILED))
    written = _checkpoint(revoked, body)
    return written or revoked.blocked(
        STAGE_PLAN_GENERATED,
        AGENT_BLOCK_BLOCKER_REASON,
        f"the resolutions revision for {source} failed: {why}; the prior draft "
        "is kept and this source is not retried",
    )


def _applied(run: _Run, source: str) -> None:
    run.book.update(
        attempted=ResolutionsAttempt(source, ATTEMPT_SUCCEEDED), applied=source
    )


def _non_row_delta(
    run: _Run, target: _McpTarget, body: str, state: TrackerState, source: str
) -> GateOutcome | _Exit:
    """Revoke first, then revise, then record the applied source."""
    revoked = _revoke_approval(run)
    if isinstance(revoked, _Exit):
        return revoked
    revised = _revise_for_resolutions(revoked, target, body, state, source)
    if isinstance(revised, _Exit):
        return revised
    _applied(revoked, source)
    return GateOutcome(revoked, revised, fast_path_reason(revoked.task, revised))


def finish_row_delta(run: _Run, revised: str, source: str) -> GateOutcome | _Exit:
    """After a row revision: keep an equal approval, else revoke; reset the round."""
    if fast_path_reason(run.task, revised) == APPROVED_FINGERPRINT_MATCH:
        _applied(run, source)
        return GateOutcome(run, revised, APPROVED_FINGERPRINT_MATCH, row_delta=True)
    revoked = _revoke_approval(run)
    if isinstance(revoked, _Exit):
        return revoked
    _applied(revoked, source)
    revoked.book.update(scan_round=0)
    return GateOutcome(
        revoked, revised, fast_path_reason(revoked.task, revised), row_delta=True
    )


def _row_delta(
    run: _Run, target: _McpTarget, body: str, state: TrackerState, source: str
) -> GateOutcome | _Exit:
    revised = _revise_for_resolutions(run, target, body, state, source)
    if isinstance(revised, _Exit):
        return revised
    return finish_row_delta(run, revised, source)


def _unobserved_park(run: _Run, applied: str) -> _Exit:
    """The fail-closed park for an applied source this snapshot does not show."""
    exit_ = run.blocked(
        STAGE_PLAN_GENERATED,
        AGENT_BLOCK_BLOCKER_REASON,
        f"the applied resolutions source {applied} is not shown by this round's "
        "ticket snapshot, so cw cannot tell whether the operator changed it. The "
        "plan approval stands. To unblock: re-dispatch once the ticket snapshot "
        "is readable again, or record a row resolution with cw dev-queue "
        "resolve (a new row source takes the normal delta path).",
        retry_eligible=True,
        plan_draft_fingerprint=draft_fingerprint_on_disk(run.worktree),
    )
    token = (
        f"{UNOBSERVED_FRICTION_PREFIX}{applied} not shown by this round's snapshot "
        "— approval left standing, parked for the operator"
    )
    return dataclasses.replace(exit_, result=_with_friction(exit_.result, [token]))


def _channel_gate(
    run: _Run, target: _McpTarget, body: str, state: TrackerState, current: str
) -> GateOutcome | _Exit | None:
    """A non-row current source against the applied one; ``None``: no delta."""
    applied = run.book.block.applied
    if _is_channel_source(applied):
        if not source_observed(str(applied), state.observation):
            return _unobserved_park(run, str(applied))
        if current != applied:
            return _non_row_delta(run, target, body, state, current)
        return None
    if current != SOURCE_NONE:
        return _non_row_delta(run, target, body, state, current)
    if applied is None:
        run.book.update(applied=SOURCE_NONE)
    return None


def resume_gate(
    run: _Run, target: _McpTarget, resume: ResumeDraft, state: TrackerState
) -> GateOutcome | _Exit:
    """Classify the resumed round and run its delta branch, if any.

    Positive evidence only: a new row source is a row delta; a concrete
    comment or body source is a non-row delta when nothing channel-based was
    applied, or when the applied comment or body source is still observed and
    differs. An applied comment or body source that is not observed parks.
    """
    applied = run.book.block.applied
    current = state.source.current
    body = resume.body
    if current is None:
        if _is_channel_source(applied):
            return _unobserved_park(run, str(applied))
        return GateOutcome(run, body, "resolutions_source_unobserved")
    if current.startswith(ROW_SOURCE_PREFIX):
        if applied != current:
            return _row_delta(run, target, body, state, current)
    else:
        delta = _channel_gate(run, target, body, state, current)
        if delta is not None:
            return delta
    return GateOutcome(run, body, fast_path_reason(run.task, body))


def settle_fingerprint(
    book: RoundBook, observation: TrackerObservation, *, row_delta: bool
) -> None:
    """Reset N on a tracker or row delta; persist the fresh fingerprint (never ++)."""
    fresh = observation.fingerprint
    if row_delta or (fresh is not None and fresh != book.block.last_evaluated):
        book.update(scan_round=0)
    if fresh is not None:
        book.update(last_evaluated=fresh)


def cap_check(book: RoundBook, status: str) -> bool:
    """Count an EXIT park round; True when the cap is reached (block instead)."""
    if status not in EXIT_STATUSES:
        return False
    if book.block.scan_round >= ROUND_CAP:
        return True
    book.update(scan_round=book.block.scan_round + 1)
    return False


def unconverged_details(result: AutoDevResult, observation: TrackerObservation) -> str:
    """The capped round's blocker details: the open items verbatim, N and the state."""
    items = [
        str(item.get("question") or item.get("claim") or item.get("premise") or item)
        for item in (*result.ambiguities, *result.premises)
    ]
    comment = observation.operator_comment if observation.observed else "unobserved"
    body_sha = observation.body_sha if observation.observed else "unobserved"
    return (
        f"the resumed scan reached the round cap (N={ROUND_CAP}) with items still "
        f"open: {'; '.join(items) or 'none listed'} "
        f"(operator_comment={comment}, body_sha={body_sha})"
    )


__all__ = [
    "APPROVED_FINGERPRINT_MATCH",
    "EXIT_STATUSES",
    "ROUND_CAP",
    "GateOutcome",
    "RoundBook",
    "TrackerState",
    "cap_check",
    "fast_path_reason",
    "finish_row_delta",
    "resume_gate",
    "settle_fingerprint",
    "synthesize_fast_path_result",
    "tracker_state",
    "unconverged_details",
]
