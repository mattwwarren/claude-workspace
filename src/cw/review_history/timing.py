"""Stage time for :mod:`cw.review_history`: where a review's time went (#3792).

Pure, like the rest of the package apart from ``collect``. Splits each round's
``wall_seconds`` window, and the whole review stage, into ``queued``,
``reviewing``, ``repairing`` and ``gate`` seconds. Nothing new is recorded;
everything derives from records cw already owns:

* the row's ``task.transition`` chain (``queued`` is PENDING, ``gate`` is
  BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF, whatever the reason, and
  RUNNING is ``reviewing`` once the fix sessions are carved out of it);
* the row's FIX sessions' ``[started_at, completed_at]`` spans (``repairing``);
* the ledger's ``recorded_at`` (the round windows, shared with ``wall_seconds``).

A ``task.transition`` event is the row's only when it names this client and
ticket id and was created within the row's lifetime; a session id is no tie,
because requeue transitions do not reliably carry one. Time in two states at
once counts once, as gate, then queued, then repairing, then reviewing. A part
whose evidence is missing is null and named under ``notes``, never guessed.
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, NamedTuple

from cw.models import QueueItemStatus, Stage
from cw.models.review_history import StageSeconds
from cw.review_history.attribution import _aware, _parse_iso

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from cw.models.review_history import HistorySummary, RoundView
    from cw.review_history.inputs import HistoryInputs, RepairSpan

_REVIEW = Stage.REVIEW.value


class _Kind(StrEnum):
    """What a review-stage status segment counts as."""

    QUEUED = "queued"
    RUNNING = "running"
    GATE = "gate"


_KIND_OF_STATUS = {
    QueueItemStatus.PENDING: _Kind.QUEUED,
    QueueItemStatus.RUNNING: _Kind.RUNNING,
    QueueItemStatus.BLOCKED_ON_USER: _Kind.GATE,
    QueueItemStatus.AWAITING_OPERATOR_SIGNOFF: _Kind.GATE,
}


class _Event(NamedTuple):
    """A usable ``task.transition`` event of the row."""

    at: datetime
    status: QueueItemStatus
    stage: str


class _Segment(NamedTuple):
    """A span of time the row spent in one counted state at the review stage."""

    kind: _Kind
    start: datetime
    end: datetime


class _Span(NamedTuple):
    """A span of time a fix session of the row was running."""

    start: datetime
    end: datetime


class _Chain(NamedTuple):
    segments: list[_Segment]
    open_counted: bool
    open_skipped: bool


class _Fixes(NamedTuple):
    spans: list[_Span]
    live_counted: bool
    open_skipped: bool


class _Slice(NamedTuple):
    """One window's seconds, and whether the chain covered all of it."""

    queued: float = 0.0
    reviewing: float = 0.0
    repairing: float = 0.0
    gate: float = 0.0
    covered: bool = True
    span_overlap: bool = False


class _Rounds(NamedTuple):
    blocks: list[StageSeconds]
    uncovered: list[int]
    unusable: list[int]
    no_span: list[int]
    head_gap: bool


class StageTimed(NamedTuple):
    """The rounds and summary with their stage time, and whether events fed it."""

    rounds: list[RoundView]
    summary: HistorySummary
    events_used: bool


# --- notes (the public strings of the ``notes`` list) --------------------------------


def _numbers(rounds: Sequence[int]) -> str:
    return ", ".join(str(n) for n in rounds)


_NO_TRANSITIONS_NOTE = (
    "no task.transition event could be used for this row (the event inbox may "
    "have been pruned); queued, reviewing and gate stage timings not recorded"
)
_OPEN_SEGMENT_NOTE = (
    "the row's last status segment or a live fix session is still open and no "
    "current time was available; not counted in stage timings"
)
_HEAD_NOT_COVERED_NOTE = (
    "queued, reviewing and gate stage timings not recorded in the summary: the "
    "row's task.transition events do not reach back to the start of its review "
    "stage (the event inbox may have been partly pruned)"
)


# --- the row's status chain ---------------------------------------------------------


def _status_of(value: object) -> QueueItemStatus | None:
    if not isinstance(value, str):
        return None
    try:
        return QueueItemStatus(value)
    except ValueError:
        return None


def _usable_events(inputs: HistoryInputs) -> list[_Event]:
    """The transition events tied to this row, oldest first (a stable sort)."""
    task = inputs.task
    lower = _aware(task.created_at)
    upper = None if inputs.row_ends_at is None else _aware(inputs.row_ends_at)
    events: list[_Event] = []
    for event in inputs.transition_events:
        payload = event.payload
        at = _aware(event.created_at)
        status = _status_of(payload.get("new_status"))
        stage = payload.get("stage")
        if (
            payload.get("client") != task.client
            or payload.get("ticket_id") != task.ticket_id
            or at < lower
            or (upper is not None and at >= upper)
            or status is None
            or not isinstance(stage, str)
        ):
            continue
        events.append(_Event(at, status, stage))
    events.sort(key=lambda e: e.at)
    return events


def _review_chain(
    events: Sequence[_Event], inputs: HistoryInputs, notes: list[str]
) -> _Chain:
    """The review-stage segments of *events*, the last one closed by ``now``."""
    segments: list[_Segment] = []
    open_counted = open_skipped = False
    row_status = inputs.task.status
    for index, event in enumerate(events):
        is_last = index == len(events) - 1
        if is_last and event.status != row_status:
            notes.append(
                f"the last task.transition event for this row says "
                f"'{event.status.value}' but the row is '{row_status.value}'; "
                "that segment is not counted in stage timings"
            )
            continue
        kind = _KIND_OF_STATUS.get(event.status)
        if kind is None or event.stage != _REVIEW:
            continue
        if not is_last:
            segments.append(_Segment(kind, event.at, events[index + 1].at))
        elif inputs.now is None:
            open_skipped = True
        elif _aware(inputs.now) > event.at:
            segments.append(_Segment(kind, event.at, _aware(inputs.now)))
            open_counted = True
    return _Chain(segments, open_counted, open_skipped)


def _head_reaches_entry(events: Sequence[_Event]) -> bool:
    """Whether the first review-stage event is the PENDING that entered review.

    Every normal entry to the review stage is a transition to PENDING at stage
    ``review`` (``_advance_task_pointer``), so a chain whose first review-stage
    event is anything else lost its head.
    """
    first = next((e for e in events if e.stage == _REVIEW), None)
    return first is not None and first.status == QueueItemStatus.PENDING


# --- the row's fix sessions ---------------------------------------------------------


def _fix_spans(inputs: HistoryInputs, notes: list[str]) -> _Fixes:
    """The row's FIX sessions as spans; a live one runs to ``now``."""
    row_start = _aware(inputs.task.created_at)
    spans: list[_Span] = []
    pre_row = no_end = 0
    live_counted = open_skipped = False
    for span in inputs.repair_spans:
        started = _aware(span.started_at)
        if started < row_start:
            pre_row += 1
            continue
        ended, live = _span_end(span, inputs.now)
        if ended is None:
            if span.live:
                open_skipped = True
            else:
                no_end += 1
            continue
        live_counted = live_counted or live
        if ended > started:
            spans.append(_Span(started, ended))
    if pre_row:
        notes.append(
            f"{pre_row} fix session(s) of this client started before this row "
            "was created; not counted in repairing stage timing"
        )
    if no_end:
        notes.append(
            f"{no_end} terminal fix session(s) of this row have no "
            "completed_at; not counted in repairing stage timing"
        )
    return _Fixes(spans, live_counted, open_skipped)


def _span_end(span: RepairSpan, now: datetime | None) -> tuple[datetime | None, bool]:
    """A span's end, and whether it is *now* (a live session): None if unknown."""
    if span.ended_at is not None:
        return _aware(span.ended_at), False
    if span.live and now is not None:
        return _aware(now), True
    return None, False


# --- the boundary sweep -------------------------------------------------------------


def _kind_at(segments: Sequence[_Segment], moment: datetime) -> _Kind | None:
    for segment in segments:
        if segment.start <= moment < segment.end:
            return segment.kind
    return None


def _boundaries(
    lo: datetime,
    hi: datetime,
    segments: Sequence[_Segment],
    spans: Sequence[_Span],
) -> list[datetime]:
    moments = [t for s in segments for t in (s.start, s.end)]
    moments.extend(t for s in spans for t in (s.start, s.end))
    return sorted({lo, hi, *(t for t in moments if lo < t < hi)})


def _slice_seconds(
    lo: datetime,
    hi: datetime,
    segments: Sequence[_Segment],
    spans: Sequence[_Span],
) -> _Slice:
    """Split ``(lo, hi]`` into stage seconds; gate, queued, repairing, reviewing.

    Each elementary interval between two boundaries is classified once, by its
    midpoint, so time in two states at once counts once. ``covered`` is False
    when some interval lies in no counted status segment.
    """
    queued = reviewing = repairing = gate = 0.0
    covered = True
    overlap = False
    for a, b in pairwise(_boundaries(lo, hi, segments, spans)):
        middle = a + (b - a) / 2
        kind = _kind_at(segments, middle)
        repair = any(s.start <= middle < s.end for s in spans)
        seconds = (b - a).total_seconds()
        overlap = overlap or repair
        covered = covered and kind is not None
        if kind is _Kind.GATE:
            gate += seconds
        elif kind is _Kind.QUEUED:
            queued += seconds
        elif repair:
            repairing += seconds
        elif kind is _Kind.RUNNING:
            reviewing += seconds
    return _Slice(queued, reviewing, repairing, gate, covered, overlap)


# --- rounds and summary -------------------------------------------------------------


def _round_blocks(
    rounds: Sequence[RoundView],
    start: datetime | None,
    chain: _Chain,
    fixes: _Fixes,
) -> _Rounds:
    """Each round's block over its ``wall_seconds`` window ``(previous, recorded]``."""
    blocks: list[StageSeconds] = []
    uncovered: list[int] = []
    unusable: list[int] = []
    no_span: list[int] = []
    head_gap = False
    previous = start
    fix_before = False
    first_ledger_round = True
    for view in rounds:
        lo, hi = previous, _parse_iso(view.recorded_at)
        previous = hi
        dispatched = fix_before
        fix_before = (view.fix_dispatched or 0) > 0
        if view.source != "ledger":
            blocks.append(StageSeconds())
            continue
        if lo is None or hi is None or hi <= lo:
            first_ledger_round = False
            unusable.append(view.round)
            blocks.append(StageSeconds())
            continue
        cut = _slice_seconds(lo, hi, chain.segments, fixes.spans)
        if not cut.covered:
            uncovered.append(view.round)
            head_gap = head_gap or first_ledger_round
        first_ledger_round = False
        repairing: float | None = cut.repairing
        if dispatched and not cut.span_overlap:
            no_span.append(view.round)
            repairing = None
        blocks.append(_block(cut, repairing))
    return _Rounds(blocks, uncovered, unusable, no_span, head_gap)


def _block(cut: _Slice, repairing: float | None) -> StageSeconds:
    """*cut* as a block: the chain-derived parts only when the window was covered."""
    if not cut.covered:
        return StageSeconds(repairing=repairing)
    return StageSeconds(
        queued=cut.queued, reviewing=cut.reviewing, repairing=repairing, gate=cut.gate
    )


def _summary_block(
    chain: _Chain,
    fixes: _Fixes,
    rounds: Sequence[RoundView],
    *,
    chain_trusted: bool,
) -> StageSeconds:
    """The whole review stage: every counted segment, and the row's fix spans."""
    points = [t for s in chain.segments for t in (s.start, s.end)]
    points.extend(t for s in fixes.spans for t in (s.start, s.end))
    cut = (
        _slice_seconds(min(points), max(points), chain.segments, fixes.spans)
        if points
        else _Slice()
    )
    fix_dispatched = any((r.fix_dispatched or 0) > 0 for r in rounds)
    repairing = None if fix_dispatched and not fixes.spans else cut.repairing
    if not chain_trusted or not chain.segments:
        return StageSeconds(repairing=repairing)
    return StageSeconds(
        queued=cut.queued, reviewing=cut.reviewing, repairing=repairing, gate=cut.gate
    )


def _window_notes(rounds: _Rounds, *, has_events: bool, notes: list[str]) -> None:
    if rounds.uncovered and has_events:
        notes.append(
            "queued, reviewing and gate stage timings not recorded for "
            f"round(s) {_numbers(rounds.uncovered)}: the row's task.transition "
            "events do not cover the round's window"
        )
    if rounds.unusable:
        notes.append(
            f"stage timings not recorded for round(s) {_numbers(rounds.unusable)}: "
            "the round's wall-time window is unknown or not positive"
        )
    if rounds.no_span:
        notes.append(
            "repairing stage timing not recorded for round(s) "
            f"{_numbers(rounds.no_span)}: a fix was dispatched but no fix "
            "session of this row overlaps the round's window"
        )


def apply_stage_timing(
    inputs: HistoryInputs,
    rounds: list[RoundView],
    summary: HistorySummary,
    start: datetime | None,
    notes: list[str],
) -> StageTimed:
    """*rounds* and *summary* with their ``stage_seconds``; notes every gap.

    *start* is the attributed ``s3_review_started`` moment that opens round 1's
    window, the one ``wall_seconds`` uses.
    """
    events = _usable_events(inputs)
    unused = len(inputs.transition_events) - len(events)
    if unused:
        notes.append(
            f"{unused} task.transition event(s) for ticket id "
            f"'{inputs.task.ticket_id}' could not be tied to this row or carry "
            "an unusable payload; not used"
        )
    if not events:
        notes.append(_NO_TRANSITIONS_NOTE)
    chain = _review_chain(events, inputs, notes)
    fixes = _fix_spans(inputs, notes)
    if chain.open_skipped or fixes.open_skipped:
        notes.append(_OPEN_SEGMENT_NOTE)
    blocks = _round_blocks(rounds, start, chain, fixes)
    _window_notes(blocks, has_events=bool(events), notes=notes)
    trusted = bool(events) and _head_reaches_entry(events) and not blocks.head_gap
    if events and not trusted:
        notes.append(_HEAD_NOT_COVERED_NOTE)
    stage_seconds = _summary_block(chain, fixes, rounds, chain_trusted=trusted)
    reported = trusted and bool(chain.segments)
    open_counted = (chain.open_counted and reported) or fixes.live_counted
    timed_rounds = [
        view.model_copy(update={"stage_seconds": block})
        for view, block in zip(rounds, blocks.blocks, strict=True)
    ]
    timed_summary = summary.model_copy(
        update={
            "stage_seconds": stage_seconds,
            "stage_seconds_as_of": (
                _aware(inputs.now).isoformat()
                if open_counted and inputs.now is not None
                else None
            ),
        }
    )
    return StageTimed(timed_rounds, timed_summary, events_used=bool(events))
