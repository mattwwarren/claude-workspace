"""Per-ticket consecutive dispatch-skip streaks (#2516).

1:1 test module of ``cw.pending_skip_streak``. Pure: every row and event is
built in memory (``tests._ticket_view_helpers.make_task`` and
``tests._dispatch_tick_helpers``), so nothing touches the inbox.

Pitfall: ``TicketTask.created_at`` defaults to the real ``datetime.now(UTC)``
and the calculator ignores events older than the row, so every row here is
built with an explicit ``created_at`` before the first event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cw.dispatch import TICK_STALE_SECONDS
from cw.models import OrchestratorEvent, OrchestratorEventType, QueueItemStatus
from cw.pending_skip_streak import (
    STREAK_MAX_GAP_SECONDS,
    SkipStreak,
    pending_skip_streaks,
)
from tests._dispatch_tick_helpers import client_tick_event, skip_tick_events
from tests._ticket_view_helpers import ACME, ACME_TICKET, make_task

if TYPE_CHECKING:
    from cw.models import TicketTask

_T0 = datetime(2026, 10, 18, 11, 0, tzinfo=UTC)
_ROW_AT = _T0 - timedelta(hours=1)
_OCCUPIED = "worktree_occupied"
_RUN = 103
_OTHER = "other"


def _row(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"created_at": _ROW_AT}
    kwargs.update(overrides)
    return make_task(**kwargs)


def _occupied(
    count: int,
    start: datetime = _T0,
    *,
    client: str = ACME,
    ticket_id: str = ACME_TICKET,
) -> list[OrchestratorEvent]:
    return skip_tick_events(client, ticket_id, _OCCUPIED, count=count, start=start)


def _now_after(events: list[OrchestratorEvent], seconds: float = 5) -> datetime:
    return max(e.created_at for e in events) + timedelta(seconds=seconds)


def _single(
    rows: list[TicketTask], events: list[OrchestratorEvent], now: datetime
) -> SkipStreak:
    result = pending_skip_streaks(rows, events, now=now)
    assert list(result) == [(ACME, ACME_TICKET)]
    return result[(ACME, ACME_TICKET)]


def _raw_tick(payload: dict[str, object], at: datetime) -> OrchestratorEvent:
    return OrchestratorEvent(
        type=OrchestratorEventType.DISPATCH_TICK, payload=payload, created_at=at
    )


class TestLeadingRun:
    def test_long_run_counts_every_event(self) -> None:
        events = _occupied(_RUN)

        streak = _single([_row()], events, _now_after(events))

        assert streak == SkipStreak(
            client=ACME,
            ticket_id=ACME_TICKET,
            skip_reason=_OCCUPIED,
            consecutive=_RUN,
            first_at=events[0].created_at,
            last_at=events[-1].created_at,
            stale=False,
        )

    def test_different_reason_in_the_middle_ends_the_run(self) -> None:
        older = _occupied(5)
        mid = skip_tick_events(
            ACME,
            ACME_TICKET,
            "tracker_mcp_gate_blocked",
            count=1,
            start=older[-1].created_at + timedelta(seconds=30),
        )
        newer = _occupied(4, start=mid[0].created_at + timedelta(seconds=30))
        events = older + mid + newer

        streak = _single([_row()], events, _now_after(events))

        assert streak.consecutive == len(newer)
        assert streak.first_at == newer[0].created_at

    def test_newest_reason_sets_the_run(self) -> None:
        older = _occupied(5)
        newest = skip_tick_events(
            ACME,
            ACME_TICKET,
            "tracker_mcp_gate_blocked",
            count=2,
            start=older[-1].created_at + timedelta(seconds=30),
        )
        events = older + newest

        streak = _single([_row()], events, _now_after(events))

        assert streak.skip_reason == "tracker_mcp_gate_blocked"
        assert streak.consecutive == len(newest)

    def test_gap_over_max_ends_the_run(self) -> None:
        older = _occupied(3)
        gap = timedelta(seconds=STREAK_MAX_GAP_SECONDS + 1)
        newer = _occupied(2, start=older[-1].created_at + gap)
        events = older + newer

        streak = _single([_row()], events, _now_after(events))

        assert streak.consecutive == len(newer)

    def test_gap_of_exactly_max_does_not_end_the_run(self) -> None:
        older = _occupied(3)
        gap = timedelta(seconds=STREAK_MAX_GAP_SECONDS)
        newer = _occupied(2, start=older[-1].created_at + gap)
        events = older + newer

        streak = _single([_row()], events, _now_after(events))

        assert streak.consecutive == len(older) + len(newer)

    def test_events_older_than_the_row_are_ignored(self) -> None:
        events = _occupied(10)
        row = _row(created_at=events[4].created_at)

        streak = _single([row], events, _now_after(events))

        assert streak.consecutive == len(events) - 4
        assert streak.first_at == events[4].created_at

    def test_row_newer_than_every_event_has_no_streak(self) -> None:
        events = _occupied(3)
        row = _row(created_at=_now_after(events))

        assert pending_skip_streaks([row], events, now=_now_after(events, 10)) == {}

    def test_input_order_does_not_matter(self) -> None:
        events = _occupied(20)
        shuffled = [*reversed(events[1::2]), *events[::2]]
        now = _now_after(events)

        assert pending_skip_streaks([_row()], shuffled, now=now) == (
            pending_skip_streaks([_row()], events, now=now)
        )


class TestFiltering:
    def test_non_pending_rows_are_excluded(self) -> None:
        events = _occupied(5)
        rows = [
            _row(status=status)
            for status in (QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER)
        ]

        assert pending_skip_streaks(rows, events, now=_now_after(events)) == {}

    def test_pending_row_without_events_is_absent(self) -> None:
        assert pending_skip_streaks([_row()], [], now=_T0) == {}

    def test_client_wide_ticks_are_ignored(self) -> None:
        events = [
            client_tick_event(ACME, _T0 + timedelta(seconds=30 * i)) for i in range(5)
        ]

        assert pending_skip_streaks([_row()], events, now=_now_after(events)) == {}

    def test_client_wide_tick_between_skips_does_not_break_the_run(self) -> None:
        older = _occupied(3)
        tick = client_tick_event(ACME, older[-1].created_at + timedelta(seconds=10))
        newer = _occupied(2, start=older[-1].created_at + timedelta(seconds=30))
        events = [*older, tick, *newer]

        streak = _single([_row()], events, _now_after(events))

        assert streak.consecutive == len(older) + len(newer)

    def test_other_event_types_are_ignored(self) -> None:
        other = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_COMPLETED,
            payload={
                "client": ACME,
                "ticket_id": ACME_TICKET,
                "skip_reason": _OCCUPIED,
            },
            created_at=_T0,
        )

        assert pending_skip_streaks([_row()], [other], now=_T0) == {}

    def test_malformed_events_are_skipped(self) -> None:
        good = _occupied(2)
        at = good[-1].created_at + timedelta(seconds=10)
        malformed = [
            _raw_tick({"client": ACME, "ticket_id": 7, "skip_reason": _OCCUPIED}, at),
            _raw_tick({"client": ACME, "ticket_id": ACME_TICKET, "skip_reason": 3}, at),
            _raw_tick({"ticket_id": ACME_TICKET, "skip_reason": _OCCUPIED}, at),
        ]

        streak = _single([_row()], good + malformed, _now_after(good, 20))

        assert streak.consecutive == len(good)
        assert streak.last_at == good[-1].created_at

    def test_same_ticket_under_two_clients_stays_separate(self) -> None:
        acme = _occupied(4)
        other = _occupied(2, client=_OTHER)
        rows = [_row(), _row(client=_OTHER)]

        result = pending_skip_streaks(rows, acme + other, now=_now_after(acme))

        assert result[(ACME, ACME_TICKET)].consecutive == len(acme)
        assert result[(_OTHER, ACME_TICKET)].consecutive == len(other)

    def test_events_for_unqueued_tickets_are_ignored(self) -> None:
        events = _occupied(3, ticket_id="ACM-99")

        assert pending_skip_streaks([_row()], events, now=_now_after(events)) == {}


class TestStale:
    def test_stale_when_last_event_is_older_than_the_threshold(self) -> None:
        events = _occupied(3)
        now = _now_after(events, TICK_STALE_SECONDS + 1)

        assert _single([_row()], events, now).stale is True

    def test_not_stale_exactly_at_the_threshold(self) -> None:
        events = _occupied(3)
        now = _now_after(events, TICK_STALE_SECONDS)

        assert _single([_row()], events, now).stale is False
