"""Collect and build the history of one ticket's review (#2790).

``collect_history_inputs`` does all the I/O, and ``build_review_history`` turns
what it read into a :class:`~cw.models.review_history.ReviewHistory` with no
I/O at all. Rendering lives in :mod:`cw.review_history_render`; the command is
``cw review history`` (:mod:`cw.cli.review.history`).

Read-only by construction: no new store, no state lock, no subprocess, no
event emitted. The reads are the converge ledger and its per-round verdict
files, the session state the caller passes in, and two reads of the event
inbox (each under the inbox's own short leaf lock, as every ``read_events``
caller). Every data problem becomes a ``notes`` entry; nothing here raises on
bad data.

Sources, in precedence order: the converge ledger at
``<worktree>/.cw/review-convergence.json`` (owned by this ticket), else the
newest session ``AUTO_DEV_RESULT`` of this row (its ``review`` block and
``review_round_<n>`` friction lines); the row's ``finding_dispositions`` are
always listed on their own.

Event attribution. A ticket id is unique per client, not globally, and the
event inbox is global, so an event is used only when it can be tied to this
row. A ``stage.entered`` event must name one of the row's sessions in its
``session_id``. A ``review.finding_voided`` event carries no session, client
or worktree, so it is tied by content: its finding must already be in this
row's ledger ``settled`` or ``finding_dispositions``, and when that record's
rationale is the voided-suppression text it must name the event's operator
comment. Every other event of the ticket id is counted in ``notes`` and never
rendered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.events import read_events
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.models.enums import StageIdentifier
from cw.models.review_history import (
    DispositionView,
    HistorySummary,
    ReleaseView,
    ReviewHistory,
    RoundView,
    RoundViewKind,
    SettledView,
    SeveritySplit,
    VoidedView,
)
from cw.reconcile._shared import resolve_session_for_task
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    LEDGER_STATUS_AWAITING_CONFIRM,
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    ConvergenceError,
    classify_findings,
    finding_identity,
    from_verdict,
    load_ledger,
    round_verdict_path,
)
from cw.review_finding_dispositions import split_disposition_key
from cw.review_findings import ReviewVerdict
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorEvent,
        Session,
        TicketTask,
    )
    from cw.review_convergence import (
        ConvergenceLedger,
        RoundRecord,
        SettledEntry,
    )

#: The start of the rationale ``apply_voided_suppression`` stamps on a voided
#: finding (``cw.review_adjudication._voided._VOIDED_RATIONALE``); a test pins
#: this constant to the producer's output.
_VOIDED_RATIONALE_PREFIX = "suppressed: matches finding voided by operator comment "
_REVIEW_STARTED = StageIdentifier.S3_REVIEW_STARTED.value
_IN_FLIGHT_STATUSES = frozenset(
    {LEDGER_STATUS_IN_PROGRESS, LEDGER_STATUS_AWAITING_CONFIRM}
)
#: One ``review_round_<n>`` line of ``cw.review_convergence.friction_lines``.
_FRICTION_RE = re.compile(
    r"^review_round_(?P<round>\d+): kind=(?P<kind>loop|confirm) "
    r"backend=(?P<backend>\S*) sha=(?P<sha>\S*) "
    r"new_accepted=(?P<new>\d+) settled=(?P<settled>\d+)$"
)


@dataclass(frozen=True)
class HistoryInputs:
    """Everything :func:`build_review_history` needs, read once."""

    task: TicketTask
    ledger: ConvergenceLedger | None = None
    verdicts: Mapping[int, ReviewVerdict] = field(default_factory=dict)
    missing_verdict_rounds: tuple[int, ...] = ()
    sentinel: Mapping[str, object] | None = None
    bound_session_ids: frozenset[str] = frozenset()
    stage_events: tuple[OrchestratorEvent, ...] = ()
    voided_events: tuple[OrchestratorEvent, ...] = ()
    notes: tuple[str, ...] = ()


class _Match(NamedTuple):
    """The row record a voided event matched; severity is None for a disposition."""

    severity: str | None
    file: str
    summary: str
    rationale: str


class _Part(NamedTuple):
    rounds: list[RoundView]
    settled: list[SettledView]
    summary: HistorySummary


# --- collection (the only I/O) ---------------------------------------------------


def collect_history_inputs(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    state: CwState | None,
    *,
    worktree_override: Path | None = None,
    other_rows: int = 0,
    read_events_fn: Callable[..., list[OrchestratorEvent]] = read_events,
) -> HistoryInputs:
    """Read the ledger, verdict files, sentinel and events for *task*'s row.

    *client_cfg* is ``None`` for an unconfigured client (its derived worktree
    is then not tried); *state* is ``None`` when the session state could not
    be read. *worktree_override* replaces the automatic ledger lookup.
    *other_rows* counts the row's re-enqueued siblings, noted when above zero.
    Never raises on a data problem: each one becomes a note.
    """
    notes: list[str] = []
    if other_rows:
        notes.append(
            f"{other_rows} other dev-queue row(s) for this ticket and client; "
            "showing the one `cw dev-queue show` selects"
        )
    sessions: Sequence[Session] = [] if state is None else state.sessions
    if state is None:
        notes.append("session state unreadable; no session sentinel or event tie used")
    automatic = _automatic_candidates(task, client_cfg, state, notes)
    lookup = automatic if worktree_override is None else [worktree_override]
    ledger, ledger_path = _find_ledger(task, lookup, notes)
    verdicts: dict[int, ReviewVerdict] = {}
    missing: tuple[int, ...] = ()
    if ledger is not None and ledger_path is not None:
        verdicts, missing = _read_verdicts(ledger, ledger_path, notes)
    # The override only moves the ledger lookup; sessions bind to the row's own
    # worktrees, or an override at another ticket's checkout would pull that
    # ticket's sessions (and their review results and events) into this row.
    worktrees = frozenset(automatic)
    row_sessions = [
        s
        for s in sessions
        if s.client == task.client
        and s.worktree_path is not None
        and s.worktree_path in worktrees
    ]
    bound = {s.id for s in row_sessions}
    if task.session_id:
        bound.add(task.session_id)
    stage_events, voided_events = _read_row_events(task, read_events_fn, notes)
    return HistoryInputs(
        task=task,
        ledger=ledger,
        verdicts=verdicts,
        missing_verdict_rounds=missing,
        sentinel=_sentinel_of(task, sessions, row_sessions),
        bound_session_ids=frozenset(bound),
        stage_events=stage_events,
        voided_events=voided_events,
        notes=tuple(notes),
    )


def _automatic_candidates(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    state: CwState | None,
    notes: list[str],
) -> list[Path]:
    """Row worktree, bound session's worktree, then the derived path (deduped)."""
    candidates: list[Path] = []
    if task.worktree_path is not None:
        candidates.append(task.worktree_path)
    session = None if state is None else resolve_session_for_task(task, state)
    if session is not None and session.worktree_path is not None:
        candidates.append(session.worktree_path)
    if client_cfg is None:
        notes.append(
            f"client '{task.client}' is not configured or its config is "
            "unreadable; its derived worktree path was not tried"
        )
    else:
        branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
        candidates.append(worktree_path_for(client_cfg, branch))
    return list(dict.fromkeys(candidates))


def _find_ledger(
    task: TicketTask, candidates: Sequence[Path], notes: list[str]
) -> tuple[ConvergenceLedger | None, Path | None]:
    """The first readable ledger owned by *task*'s ticket, and its path."""
    if not candidates:
        notes.append("no worktree to look for a converge ledger in")
        return None, None
    for candidate in candidates:
        path = candidate / LEDGER_RELATIVE_PATH
        try:
            ledger = load_ledger(path)
        except (OSError, ValueError) as exc:
            notes.append(
                f"converge ledger at {path} unreadable ({type(exc).__name__}); skipped"
            )
            continue
        if ledger is None:
            continue
        if ledger.ticket_id != task.ticket_id:
            notes.append(
                f"converge ledger at {path} belongs to foreign ticket "
                f"'{ledger.ticket_id}'; skipped"
            )
            continue
        return ledger, path
    looked = ", ".join(str(c) for c in candidates)
    notes.append(f"no converge ledger found for this ticket (looked in: {looked})")
    return None, None


def _read_verdicts(
    ledger: ConvergenceLedger, ledger_path: Path, notes: list[str]
) -> tuple[dict[int, ReviewVerdict], tuple[int, ...]]:
    """Each recorded round's verdict file, plus the rounds that have none."""
    verdicts: dict[int, ReviewVerdict] = {}
    missing: list[int] = []
    for record in ledger.rounds:
        path = round_verdict_path(ledger_path, record.round)
        if not path.exists():
            missing.append(record.round)
            continue
        try:
            verdicts[record.round] = ReviewVerdict.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            notes.append(
                f"round {record.round} verdict file {path} unreadable "
                f"({type(exc).__name__}); severity split not shown"
            )
    return verdicts, tuple(missing)


def _review_block(result: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if result is None:
        return None
    review = result.get("review")
    return review if isinstance(review, dict) else None


def _sentinel_of(
    task: TicketTask, sessions: Sequence[Session], row_sessions: Sequence[Session]
) -> Mapping[str, object] | None:
    """The bound session's result, else the newest row session's with a review."""
    if task.session_id:
        bound = next((s for s in sessions if s.id == task.session_id), None)
        if bound is not None and _review_block(bound.last_result) is not None:
            return bound.last_result
    reviewed = [s for s in row_sessions if _review_block(s.last_result) is not None]
    if not reviewed:
        return None
    return max(reviewed, key=lambda s: s.started_at).last_result


def _read_row_events(
    task: TicketTask,
    read_events_fn: Callable[..., list[OrchestratorEvent]],
    notes: list[str],
) -> tuple[tuple[OrchestratorEvent, ...], tuple[OrchestratorEvent, ...]]:
    """This ticket id's review-start and voided events, not yet attributed."""
    try:
        stage = read_events_fn(event_types=[OrchestratorEventType.STAGE_ENTERED])
        voided = read_events_fn(
            event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED]
        )
    except (CwError, OSError, ValueError) as exc:
        notes.append(
            f"event inbox unreadable ({type(exc).__name__}); round-1 wall time "
            "and voided findings not shown"
        )
        return (), ()
    return (
        tuple(
            e
            for e in stage
            if e.payload.get("stage") == _REVIEW_STARTED
            and e.payload.get("ticket_id") == task.ticket_id
        ),
        tuple(e for e in voided if e.correlation_id == task.ticket_id),
    )


# --- building (pure) ---------------------------------------------------------------


def build_review_history(inputs: HistoryInputs) -> ReviewHistory:
    """Turn *inputs* into the documented view; pure, never raises on bad data."""
    task = inputs.task
    notes = list(inputs.notes)
    review = _review_block(inputs.sentinel)
    if inputs.ledger is not None:
        part = _ledger_part(inputs.ledger, inputs, notes)
        _cross_check(inputs.ledger, review, notes)
    else:
        part = _sentinel_part(inputs.sentinel, review, notes)
    round_one = part.rounds[0] if part.rounds else None
    round_one_at = (
        _parse_iso(round_one.recorded_at)
        if round_one is not None and round_one.source == "ledger"
        else None
    )
    start = _attributed_stage_start(inputs, round_one_at, notes)
    if round_one_at is not None and start is None:
        notes.append(
            "round 1 wall time not recorded: no s3_review_started event from "
            "this row's sessions precedes it"
        )
    voided = _attribute_voided(inputs, notes)
    return ReviewHistory(
        ticket_id=task.ticket_id,
        client=task.client,
        row_status=task.status.value,
        row_stage=task.stage.value,
        in_flight=_in_flight(inputs),
        rounds=_with_wall_times(part.rounds, start),
        summary=part.summary,
        settled=part.settled,
        dispositions=_disposition_views(task),
        voided=voided,
        sources=_sources(
            inputs, part, review, events_used=start is not None or bool(voided)
        ),
        notes=notes,
    )


def _in_flight(inputs: HistoryInputs) -> bool:
    if inputs.ledger is not None:
        return inputs.ledger.status in _IN_FLIGHT_STATUSES
    task = inputs.task
    return task.status == QueueItemStatus.RUNNING and task.stage == Stage.REVIEW


def _sources(
    inputs: HistoryInputs,
    part: _Part,
    review: Mapping[str, object] | None,
    *,
    events_used: bool,
) -> list[str]:
    sources: list[str] = []
    if inputs.ledger is not None:
        sources.append("ledger")
        if any(r.new_by_severity is not None for r in part.rounds):
            sources.append("verdict_files")
    elif review is not None or part.rounds:
        sources.append("sentinel")
    if inputs.task.finding_dispositions:
        sources.append("row_dispositions")
    if events_used:
        sources.append("events")
    return sources


# --- the ledger path -------------------------------------------------------------


def _ledger_part(
    ledger: ConvergenceLedger, inputs: HistoryInputs, notes: list[str]
) -> _Part:
    settled = [_settled_view(entry) for entry in ledger.settled]
    numbers = {record.round for record in ledger.rounds}
    by_round: dict[int, list[SettledView]] = {}
    for view, entry in zip(settled, ledger.settled, strict=True):
        if view.origin != "adjudication":
            continue
        if view.round in numbers:
            by_round.setdefault(view.round, []).append(view)
        else:
            notes.append(
                f"settled entry {entry.identity.audit_key()} names round "
                f"{view.round}, which has no recorded round; listed under "
                "settled only"
            )
    rounds = [
        _ledger_round_view(
            record,
            by_round.get(record.round, []),
            _severity_split(ledger, record, inputs.verdicts.get(record.round), notes),
        )
        for record in ledger.rounds
    ]
    if inputs.missing_verdict_rounds:
        listed = ", ".join(str(n) for n in inputs.missing_verdict_rounds)
        notes.append(
            f"round(s) {listed}: no per-round verdict file (a findings-form round, "
            "or the file was removed); severity split not recorded"
        )
    if not ledger.rounds:
        notes.append("the converge ledger has no recorded round yet")
    return _Part(rounds, settled, _ledger_summary(ledger, rounds, settled))


def _settled_view(entry: SettledEntry) -> SettledView:
    identity = entry.identity
    return SettledView(
        severity=identity.severity,
        file=identity.file,
        summary=identity.summary_normalized,
        outcome=entry.outcome,
        rationale=entry.rationale,
        round=entry.round,
        origin=entry.origin,
        actor=entry.actor,
        settled_at=entry.settled_at,
        reviewed_sha=entry.reviewed_sha,
    )


def _ledger_round_view(
    record: RoundRecord, settled: list[SettledView], split: SeveritySplit | None
) -> RoundView:
    return RoundView(
        round=record.round,
        kind=record.kind,
        source="ledger",
        backend=record.backend,
        reviewed_sha=record.reviewed_sha,
        roster=list(record.roster),
        new_accepted=record.new_accepted,
        new_fix=record.new_fix,
        new_operator_action=record.new_operator_action,
        new_settled=record.new_settled,
        resurfaced_settled=record.resurfaced_settled,
        settled_total=record.new_settled + record.resurfaced_settled,
        new_by_severity=split,
        fix_dispatched=record.new_fix,
        clean=record.clean,
        recorded_at=record.recorded_at,
        settled=settled,
    )


def _severity_split(
    ledger: ConvergenceLedger,
    record: RoundRecord,
    verdict: ReviewVerdict | None,
    notes: list[str],
) -> SeveritySplit | None:
    """MUST_FIX/SHOULD_FIX split of round *record*'s new findings, if it checks out.

    Runs the engine's own new-accepted rule (``classify_findings``) over the
    round's verdict file against the ledger as it stood before this round
    (settled entries of earlier rounds only), and returns the split only when
    its total equals the ledger's own ``new_fix + new_operator_action``.
    """
    if verdict is None:
        return None
    try:
        findings, _ = from_verdict(verdict)
    except ConvergenceError:
        notes.append(
            f"round {record.round}: verdict file holds an undecided finding; "
            "severity split not shown"
        )
        return None
    before = ledger.model_copy(
        update={"settled": [e for e in ledger.settled if e.round < record.round]}
    )
    classified = classify_findings(before, findings, record.round)
    new = [*classified.new_fix, *classified.new_operator]
    expected = record.new_fix + record.new_operator_action
    if len(new) != expected:
        notes.append(
            f"round {record.round}: verdict file has {len(new)} new MUST_FIX/"
            f"SHOULD_FIX finding(s) but the ledger recorded {expected}; "
            "severity split not shown"
        )
        return None
    must = sum(1 for f in new if f.severity == "MUST_FIX")
    return SeveritySplit(must_fix=must, should_fix=len(new) - must)


def _ledger_summary(
    ledger: ConvergenceLedger, rounds: list[RoundView], settled: list[SettledView]
) -> HistorySummary:
    converged = ledger.status == LEDGER_STATUS_CONVERGED
    return HistorySummary(
        convergence_mode="clean",
        rounds_run=len(ledger.rounds),
        converged=converged,
        final_clean_sha=ledger.last_clean_sha if converged else None,
        ledger_status=ledger.status,
        stop_reason=ledger.stop_reason,
        stop_identities=[i.audit_key() for i in ledger.stop_identities],
        fix_cycles_used=ledger.fix_cycles_used,
        max_cycles=ledger.max_cycles,
        confirm_backend=ledger.confirm_backend,
        new_accepted_series=_series(rounds),
        deferred=[s for s in settled if s.outcome == "defer"],
        releases=[
            ReleaseView(
                at=r.at, round=r.round, reason=r.reason, cause=r.cause, sha=r.sha
            )
            for r in ledger.releases
        ],
        confirm_failures=len(ledger.confirm_failures),
    )


def _cross_check(
    ledger: ConvergenceLedger, review: Mapping[str, object] | None, notes: list[str]
) -> None:
    rounds_run = None if review is None else _int(review.get("rounds_run"))
    if rounds_run is not None and rounds_run != len(ledger.rounds):
        notes.append(
            f"sentinel rounds_run={rounds_run} disagrees with the ledger's "
            f"{len(ledger.rounds)} round(s); the ledger wins"
        )


def _series(rounds: Sequence[RoundView]) -> list[int]:
    return [r.new_accepted for r in rounds if r.new_accepted is not None]


# --- the sentinel path -------------------------------------------------------------


def _sentinel_part(
    sentinel: Mapping[str, object] | None,
    review: Mapping[str, object] | None,
    notes: list[str],
) -> _Part:
    rounds = _sentinel_rounds(sentinel, review, notes)
    if review is None:
        summary = HistorySummary(
            convergence_mode="unknown", new_accepted_series=_series(rounds)
        )
        return _Part(rounds, [], summary)
    deferred = _int(review.get("deferred"))
    if deferred:
        notes.append(
            f"sentinel reports {deferred} deferred finding(s); their text is "
            "recorded only in a converge ledger"
        )
    final_sha = review.get("final_round_clean_sha")
    backend = review.get("confirm_backend")
    converged = review.get("converged")
    summary = HistorySummary(
        convergence_mode=(
            "clean" if review.get("convergence_mode") == "clean" else "blocking"
        ),
        rounds_run=_int(review.get("rounds_run")),
        converged=converged if isinstance(converged, bool) else None,
        final_clean_sha=final_sha if isinstance(final_sha, str) else None,
        fix_cycles_used=_int(review.get("fix_cycles_used")),
        confirm_backend=backend if isinstance(backend, str) else None,
        new_accepted_series=_series(rounds),
    )
    return _Part(rounds, [], summary)


def _sentinel_rounds(
    sentinel: Mapping[str, object] | None,
    review: Mapping[str, object] | None,
    notes: list[str],
) -> list[RoundView]:
    friction = sentinel.get("friction_highlights") if sentinel is not None else None
    rounds = _parse_friction_lines(friction if isinstance(friction, list) else [])
    if rounds:
        return rounds
    if review is None:
        notes.append(
            "no session AUTO_DEV_RESULT with a review block found for this row"
        )
        return []
    if review.get("convergence_mode") == "clean":
        notes.append(
            "the sentinel's review block is clean-mode but carries no "
            "review_round_<n> friction lines; per-round data not recorded"
        )
        return []
    notes.append(
        "rounds_unrecorded: a blocking-mode review records no per-round data; "
        "per-round history needs review.convergence: clean"
    )
    cycle0 = _blocking_cycle0(review)
    if cycle0 is None:
        notes.append(
            "the sentinel's review block carries no must_fix_initial/should_fix"
        )
        return []
    return [cycle0]


def _parse_friction_lines(lines: Sequence[object]) -> list[RoundView]:
    """Round views from ``review_round_<n>`` friction lines; others are ignored.

    The inverse of ``cw.review_convergence.friction_lines`` for the round
    lines (a test round-trips the two). The first line per round wins.
    """
    views: list[RoundView] = []
    seen: set[int] = set()
    for line in lines:
        match = _FRICTION_RE.match(line.strip()) if isinstance(line, str) else None
        if match is None or int(match["round"]) in seen:
            continue
        number = int(match["round"])
        seen.add(number)
        new = int(match["new"])
        kind: RoundViewKind = "confirm" if match["kind"] == "confirm" else "loop"
        views.append(
            RoundView(
                round=number,
                kind=kind,
                source="sentinel_friction",
                backend=match["backend"] or None,
                reviewed_sha=match["sha"] or None,
                new_accepted=new,
                settled_total=int(match["settled"]),
                clean=new == 0,
            )
        )
    return views


def _blocking_cycle0(review: Mapping[str, object]) -> RoundView | None:
    must = _int(review.get("must_fix_initial"))
    should = _int(review.get("should_fix"))
    if must is None or should is None:
        return None
    return RoundView(
        round=1,
        kind="initial",
        source="sentinel_cycle0",
        new_accepted=must + should,
        new_by_severity=SeveritySplit(must_fix=must, should_fix=should),
    )


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- wall time and stage.entered attribution ----------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    """*value* as an aware datetime (naive reads as UTC), or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _attributed_stage_start(
    inputs: HistoryInputs, round_one_at: datetime | None, notes: list[str]
) -> datetime | None:
    """Latest attributable review start at or before round 1; counts the rest."""
    attributable: list[datetime] = []
    foreign = 0
    for event in inputs.stage_events:
        session_id = event.payload.get("session_id")
        if (
            isinstance(session_id, str)
            and session_id
            and session_id in inputs.bound_session_ids
        ):
            attributable.append(_aware(event.created_at))
        else:
            foreign += 1
    if foreign:
        notes.append(
            f"{foreign} stage.entered event(s) for ticket id "
            f"'{inputs.task.ticket_id}' could not be tied to this row's "
            "sessions; not used"
        )
    if round_one_at is None:
        return None
    starts = [moment for moment in attributable if moment <= round_one_at]
    return max(starts) if starts else None


def _with_wall_times(
    rounds: list[RoundView], start: datetime | None
) -> list[RoundView]:
    timed: list[RoundView] = []
    previous = start
    for view in rounds:
        current = _parse_iso(view.recorded_at)
        wall = (
            (current - previous).total_seconds()
            if current is not None and previous is not None
            else None
        )
        timed.append(view.model_copy(update={"wall_seconds": wall}))
        previous = current
    return timed


# --- review.finding_voided attribution and row dispositions --------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _payload_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _attribute_voided(inputs: HistoryInputs, notes: list[str]) -> list[VoidedView]:
    views: list[VoidedView] = []
    for event in inputs.voided_events:
        view = _voided_view(event, inputs)
        if view is not None:
            views.append(view)
    foreign = len(inputs.voided_events) - len(views)
    if foreign:
        notes.append(
            f"{foreign} review.finding_voided event(s) for ticket id "
            f"'{inputs.task.ticket_id}' could not be tied to this row; not shown"
        )
    return views


def _voided_view(event: OrchestratorEvent, inputs: HistoryInputs) -> VoidedView | None:
    """The view of *event* when it ties to one of this row's records, else None."""
    payload = event.payload
    severity = _payload_str(payload, "severity")
    file = _payload_str(payload, "file")
    summary = _payload_str(payload, "summary")
    comment = _payload_str(payload, "operator_comment_id")
    if not (severity and file and summary and comment):
        return None
    match = _ledger_match(inputs.ledger, severity, file, summary) or _disposition_match(
        inputs.task, file, summary
    )
    if match is None:
        return None
    if match.rationale.startswith(_VOIDED_RATIONALE_PREFIX) and (
        comment not in match.rationale
    ):
        return None
    return VoidedView(
        severity=match.severity or severity,
        file=match.file,
        summary=match.summary,
        original_rationale=_payload_str(payload, "original_rationale"),
        voided_at=_payload_str(payload, "voided_at"),
        operator_comment_id=comment,
        event_at=_aware(event.created_at).isoformat(),
    )


def _ledger_match(
    ledger: ConvergenceLedger | None, severity: str, file: str, summary: str
) -> _Match | None:
    if ledger is None:
        return None
    for entry in ledger.settled:
        identity = entry.identity
        if identity.severity == severity and identity == finding_identity(
            identity.severity, file, summary
        ):
            return _Match(
                identity.severity,
                identity.file,
                identity.summary_normalized,
                entry.rationale,
            )
    return None


def _disposition_match(task: TicketTask, file: str, summary: str) -> _Match | None:
    wanted = _normalize(summary)
    for key, entry in sorted(task.finding_dispositions.items()):
        if (
            entry.summary
            and split_disposition_key(key)[0] == file
            and _normalize(entry.summary) == wanted
        ):
            return _Match(None, file, entry.summary, entry.rationale)
    return None


def _disposition_views(task: TicketTask) -> list[DispositionView]:
    return [
        DispositionView(
            key=key,
            file=split_disposition_key(key)[0],
            summary=entry.summary,
            outcome=entry.outcome,
            rationale=entry.rationale,
            actor=entry.actor,
            recorded_at=entry.recorded_at,
            reviewed_sha=entry.reviewed_sha,
        )
        for key, entry in sorted(task.finding_dispositions.items())
    ]
