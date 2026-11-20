"""Tests for ``cw.review_history.timing``: the stage-time split (#3792).

Every ledger is built through the real engine, every ``task.transition`` event
by the real ``transition_task_status`` and every fix session by the real
``Session`` model, then read back through the real collector and builder (the
conventions of ``tests/_review_history_helpers.py``). The clock is frozen
around the collection, which is what stamps the report's ``now``.

The fixture timeline (see ``FIX_CYCLE_STEPS``), all 2026-10-27::

    11:00 PENDING   11:30 RUNNING   11:50 review start   12:00 round 1
    12:05-12:25 fix session   12:25:30 PENDING   12:30 RUNNING   12:50 round 2
    13:00 BLOCKED_ON_USER   14:02 RUNNING   14:10 COMPLETED
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.events import record_event
from cw.models import (
    CwState,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    Stage,
)
from cw.review_convergence import ConvergenceLedger
from cw.review_history import build_review_history, collect_history_inputs
from tests._review_convergence_helpers import fresh_ledger, record, round_finding
from tests._review_history_helpers import (
    ACME_SESSION,
    FIX_CYCLE_STEPS,
    FIX_SESSION_COMPLETED,
    NOTE_HEAD_NOT_COVERED,
    NOTE_NO_TRANSITIONS,
    NOTE_OPEN_SEGMENT,
    OTHER,
    Step,
    client_config,
    derived_worktree,
    fix_cycle_ledger,
    fix_session_for,
    fixture_blocking_sentinel,
    fixture_in_progress,
    history_for,
    note_fix_no_completed_at,
    note_foreign_transitions,
    note_last_mismatch,
    note_no_fix_span,
    note_pre_row_fix,
    note_uncovered,
    note_unusable,
    seed_fix_cycle,
    session_for,
    stage_entered_event,
    status_chain,
    timed,
    timing_task,
    transition_event,
)
from tests._ticket_view_helpers import ACME_TICKET, bound_session_for

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session, TicketTask
    from cw.models.review_history import ReviewHistory, StageSeconds

#: The frozen report time: after the whole fixture timeline.
NOW = "2026-10-27T15:00:00+00:00"
_ROUND_STAMP = "2026-10-27T12:00:00+00:00"
_RUNNING = QueueItemStatus.RUNNING
_PENDING = QueueItemStatus.PENDING


def _parts(block: StageSeconds) -> dict[str, float | None]:
    return block.model_dump()


def _expected(
    queued: float | None,
    reviewing: float | None,
    repairing: float | None,
    gate: float | None,
) -> dict[str, float | None]:
    return {
        "queued": queued,
        "reviewing": reviewing,
        "repairing": repairing,
        "gate": gate,
    }


_ALL_NULL = _expected(None, None, None, None)


def _timing_notes(history: ReviewHistory) -> list[str]:
    return [n for n in history.notes if "stage timing" in n or "task.transition" in n]


def _history(
    tmp_path: Path,
    task: TicketTask,
    sessions: list[Session],
    *,
    now: str = NOW,
    **extra: object,
) -> ReviewHistory:
    return history_for(task, tmp_path, sessions=sessions, now=now, **extra)


def _history_without_now(
    tmp_path: Path, task: TicketTask, sessions: list[Session]
) -> ReviewHistory:
    """The history of a report built with no current time (the library path)."""
    with freeze_time(NOW):
        inputs = collect_history_inputs(
            task, client_config(tmp_path), CwState(sessions=sessions)
        )
    return build_review_history(replace(inputs, now=None))


def _steps(replace_status: dict[int, Step]) -> list[Step]:
    steps = list(FIX_CYCLE_STEPS)
    for index, step in replace_status.items():
        steps[index] = step
    return steps


def test_full_chain_splits_every_round_and_the_summary(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path)

    history = _history(tmp_path, task, sessions)

    round_one, round_two = history.rounds
    assert (round_one.wall_seconds, round_two.wall_seconds) == (600.0, 3000.0)
    assert _parts(round_one.stage_seconds) == _expected(0.0, 600.0, 0.0, 0.0)
    assert _parts(round_two.stage_seconds) == _expected(270.0, 1530.0, 1200.0, 0.0)
    for view in history.rounds:
        total = sum(v for v in _parts(view.stage_seconds).values() if v is not None)
        assert total == view.wall_seconds
    assert _parts(history.summary.stage_seconds) == _expected(
        2070.0, 4410.0, 1200.0, 3720.0
    )
    assert history.summary.stage_seconds_as_of is None
    assert _timing_notes(history) == []
    assert "events" in history.sources
    assert history.schema_version == 1


def test_wall_seconds_is_unchanged_by_the_stage_split(tmp_path: Path) -> None:
    """The same ledger with no transition events keeps its wall times."""
    task, sessions = seed_fix_cycle(tmp_path, steps=())

    history = _history(tmp_path, task, sessions)

    assert [r.wall_seconds for r in history.rounds] == [600.0, 3000.0]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, None),
        (QueueItemStatus.BLOCKED_ON_USER, "review_pending_approval"),
        (QueueItemStatus.BLOCKED_ON_USER, "phantom_session_reaped"),
        (QueueItemStatus.BLOCKED_ON_USER, "usage_limit_crash_park"),
    ],
)
def test_every_operator_park_counts_as_gate_whatever_its_reason(
    tmp_path: Path, status: QueueItemStatus, reason: str | None
) -> None:
    """R1: no reason taxonomy; a crash or reap park is gate time too."""
    park = Step(FIX_CYCLE_STEPS[4].at, status, blocked_reason=reason)
    task, sessions = seed_fix_cycle(tmp_path, _steps({4: park}))

    history = _history(tmp_path, task, sessions)

    assert history.summary.stage_seconds.gate == 3720.0
    assert _timing_notes(history) == []


def test_a_terminal_segment_inside_a_window_leaves_it_uncovered(
    tmp_path: Path,
) -> None:
    steps = [
        *FIX_CYCLE_STEPS[:2],
        Step("2026-10-27T12:20:00+00:00", QueueItemStatus.COMPLETED),
    ]
    task, sessions = seed_fix_cycle(tmp_path, steps)

    history = _history(tmp_path, task, sessions)

    round_one, round_two = history.rounds
    assert _parts(round_one.stage_seconds) == _expected(0.0, 600.0, 0.0, 0.0)
    # The fix span is session-derived, so it still counts although the chain ends.
    assert _parts(round_two.stage_seconds) == _expected(None, None, 1200.0, None)
    assert note_uncovered("2") in history.notes
    assert _parts(history.summary.stage_seconds) == _expected(
        1800.0, 2100.0, 1200.0, 0.0
    )


def test_an_open_park_is_counted_to_now_and_says_so(tmp_path: Path) -> None:
    steps = [*FIX_CYCLE_STEPS[:2], FIX_CYCLE_STEPS[4]]
    _, sessions = seed_fix_cycle(tmp_path, steps)
    parked = timing_task(status=QueueItemStatus.BLOCKED_ON_USER)

    history = _history(tmp_path, parked, sessions)

    assert history.summary.stage_seconds.gate == 7200.0
    assert history.summary.stage_seconds_as_of == NOW
    assert _timing_notes(history) == []


def test_an_open_segment_with_no_now_is_not_counted(tmp_path: Path) -> None:
    steps = [*FIX_CYCLE_STEPS[:2], FIX_CYCLE_STEPS[4]]
    _, sessions = seed_fix_cycle(tmp_path, steps)
    parked = timing_task(status=QueueItemStatus.BLOCKED_ON_USER)

    history = _history_without_now(tmp_path, parked, sessions)

    assert history.summary.stage_seconds.gate == 0.0
    assert history.summary.stage_seconds_as_of is None
    assert NOTE_OPEN_SEGMENT in history.notes


def test_a_live_fix_session_is_counted_to_now(tmp_path: Path) -> None:
    fixture_in_progress().write(derived_worktree(tmp_path))
    task = timing_task(status=_RUNNING)
    stage_entered_event(ACME_TICKET, ACME_SESSION, "2026-10-27T11:50:00+00:00")
    status_chain(task, *FIX_CYCLE_STEPS[:2])
    live = fix_session_for(
        task,
        "sess-live",
        "2026-10-27T12:05:00+00:00",
        worktree=derived_worktree(tmp_path),
    )

    history = _history(tmp_path, task, [live], now="2026-10-27T12:30:00+00:00")

    assert _parts(history.summary.stage_seconds) == _expected(
        1800.0, 2100.0, 1500.0, 0.0
    )
    assert history.summary.stage_seconds_as_of == "2026-10-27T12:30:00+00:00"
    assert _timing_notes(history) == []


def test_a_live_fix_session_with_no_now_is_not_counted(tmp_path: Path) -> None:
    fixture_in_progress().write(derived_worktree(tmp_path))
    task = timing_task(status=_RUNNING)
    status_chain(task, *FIX_CYCLE_STEPS[:2])
    live = fix_session_for(
        task,
        "sess-live",
        "2026-10-27T12:05:00+00:00",
        worktree=derived_worktree(tmp_path),
    )

    history = _history_without_now(tmp_path, task, [live])

    assert NOTE_OPEN_SEGMENT in history.notes
    assert history.summary.stage_seconds_as_of is None


def test_a_last_transition_that_disagrees_with_the_row_is_dropped(
    tmp_path: Path,
) -> None:
    task, sessions = seed_fix_cycle(tmp_path, FIX_CYCLE_STEPS[:-1])

    history = _history(tmp_path, task, sessions)

    assert note_last_mismatch("running", "completed") in history.notes
    assert history.summary.stage_seconds_as_of is None
    assert _parts(history.summary.stage_seconds) == _expected(
        2070.0, 3930.0, 1200.0, 3720.0
    )


def test_events_that_are_not_this_rows_are_ignored_and_counted(
    tmp_path: Path,
) -> None:
    task, sessions = seed_fix_cycle(tmp_path)
    transition_event(task, _RUNNING, "2026-10-27T12:10:00+00:00", client=OTHER)
    transition_event(task, _RUNNING, "2026-10-27T10:00:00+00:00")
    transition_event(task, _RUNNING, "2026-10-27T14:40:00+00:00")
    for at, status in (
        ("2026-10-27T12:11:00+00:00", 7),
        ("2026-10-27T12:12:00+00:00", "bogus"),
    ):
        with freeze_time(at):
            record_event(
                OrchestratorEventType.TASK_TRANSITION,
                {
                    "ticket_id": ACME_TICKET,
                    "client": task.client,
                    "stage": "review",
                    "new_status": status,
                },
                correlation_id=ACME_TICKET,
            )

    history = _history(
        tmp_path,
        task,
        sessions,
        row_ends_at=datetime(2026, 10, 27, 14, 30, tzinfo=UTC),
    )

    assert note_foreign_transitions(5) in history.notes
    assert _parts(history.summary.stage_seconds) == _expected(
        2070.0, 4410.0, 1200.0, 3720.0
    )
    assert _parts(history.rounds[1].stage_seconds) == _expected(
        270.0, 1530.0, 1200.0, 0.0
    )


def test_no_transition_events_leave_only_the_session_derived_part(
    tmp_path: Path,
) -> None:
    task, sessions = seed_fix_cycle(tmp_path, steps=())

    history = _history(tmp_path, task, sessions)

    round_one, round_two = history.rounds
    assert _parts(round_one.stage_seconds) == _expected(None, None, 0.0, None)
    assert _parts(round_two.stage_seconds) == _expected(None, None, 1200.0, None)
    assert _parts(history.summary.stage_seconds) == _expected(None, None, 1200.0, None)
    assert history.notes.count(NOTE_NO_TRANSITIONS) == 1
    assert NOTE_HEAD_NOT_COVERED not in history.notes
    assert note_uncovered("1, 2") not in history.notes


def test_an_unusable_window_gives_an_all_null_block(tmp_path: Path) -> None:
    """R <= P: round 2 is recorded at the same instant as round 1."""
    task, sessions = seed_fix_cycle(
        tmp_path, ledger=timed(fix_cycle_ledger(), _ROUND_STAMP, _ROUND_STAMP)
    )

    history = _history(tmp_path, task, sessions)

    assert _parts(history.rounds[0].stage_seconds) == _expected(0.0, 600.0, 0.0, 0.0)
    assert _parts(history.rounds[1].stage_seconds) == _ALL_NULL
    assert note_unusable("2") in history.notes


def test_an_unparseable_record_time_makes_that_and_the_next_window_unusable(
    tmp_path: Path,
) -> None:
    task, sessions = seed_fix_cycle(
        tmp_path, ledger=timed(fix_cycle_ledger(), "not-a-time")
    )

    history = _history(tmp_path, task, sessions)

    assert [_parts(r.stage_seconds) for r in history.rounds] == [_ALL_NULL] * 2
    assert note_unusable("1, 2") in history.notes


def test_round_one_without_an_attributable_start_is_unusable(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path, with_start=False)

    history = _history(tmp_path, task, sessions)

    assert _parts(history.rounds[0].stage_seconds) == _ALL_NULL
    assert _parts(history.rounds[1].stage_seconds) == _expected(
        270.0, 1530.0, 1200.0, 0.0
    )
    assert note_unusable("1") in history.notes
    assert "events" in history.sources


def test_a_terminal_fix_session_without_completed_at_is_not_counted(
    tmp_path: Path,
) -> None:
    task, _ = seed_fix_cycle(tmp_path)
    broken = fix_session_for(
        task,
        "sess-fix",
        "2026-10-27T12:05:00+00:00",
        worktree=derived_worktree(tmp_path),
        status=SessionStatus.COMPLETED,
    )

    history = _history(tmp_path, task, [broken])

    assert note_fix_no_completed_at(1) in history.notes
    assert note_no_fix_span("2") in history.notes
    assert history.rounds[1].stage_seconds.repairing is None
    assert history.summary.stage_seconds.repairing is None
    assert history.summary.stage_seconds.reviewing == 5610.0


def test_a_fix_session_that_predates_the_row_is_excluded(tmp_path: Path) -> None:
    task, _ = seed_fix_cycle(tmp_path)
    early = fix_session_for(
        task,
        "sess-early",
        "2026-10-27T10:00:00+00:00",
        "2026-10-27T10:30:00+00:00",
        worktree=derived_worktree(tmp_path),
    )

    history = _history(tmp_path, task, [early])

    assert note_pre_row_fix(1) in history.notes
    assert history.rounds[1].stage_seconds.repairing is None
    assert note_no_fix_span("2") in history.notes


def test_other_purposes_and_other_clients_are_not_repair_time(tmp_path: Path) -> None:
    task, sessions = seed_fix_cycle(tmp_path)
    worktree = derived_worktree(tmp_path)
    impl = session_for(
        task,
        "sess-impl",
        worktree_path=worktree,
        started_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 10, 27, 12, 10, tzinfo=UTC),
        status=SessionStatus.COMPLETED,
    )
    stranger = fix_session_for(
        task,
        "sess-other",
        "2026-10-27T12:00:00+00:00",
        "2026-10-27T12:10:00+00:00",
        worktree=worktree,
        client=OTHER,
    )

    history = _history(tmp_path, task, [*sessions, impl, stranger])

    assert history.rounds[1].stage_seconds.repairing == 1200.0
    assert history.summary.stage_seconds.repairing == 1200.0


def test_a_fix_dispatched_with_no_span_is_null_not_zero(tmp_path: Path) -> None:
    task, _ = seed_fix_cycle(tmp_path)

    history = _history(tmp_path, task, [])

    assert history.rounds[0].stage_seconds.repairing == 0.0
    assert history.rounds[1].stage_seconds.repairing is None
    assert history.summary.stage_seconds.repairing is None
    assert note_no_fix_span("2") in history.notes
    assert _parts(history.rounds[1].stage_seconds) == _expected(
        270.0, 2730.0, None, 0.0
    )


def _clean_ledger() -> ConvergenceLedger:
    """A clean loop round then a clean confirm: no fix was ever dispatched."""
    ledger = fresh_ledger(confirm="codex", ticket_id=ACME_TICKET)
    ledger, _ = record(ledger, sha="sha-0001")
    ledger, _ = record(ledger, sha="sha-0001", kind="confirm")
    assert ledger.rounds[0].new_fix == 0
    return timed(ledger, "2026-10-27T12:00:00+00:00", "2026-10-27T12:50:00+00:00")


def test_no_fix_dispatched_is_zero_repair(tmp_path: Path) -> None:
    task, _ = seed_fix_cycle(tmp_path, ledger=_clean_ledger())

    history = _history(tmp_path, task, [])

    assert _parts(history.rounds[1].stage_seconds) == _expected(270.0, 2730.0, 0.0, 0.0)
    assert history.summary.stage_seconds.repairing == 0.0
    assert note_no_fix_span("2") not in history.notes


def test_a_fix_span_overlapping_a_gate_is_counted_once_as_gate(
    tmp_path: Path,
) -> None:
    steps = [
        FIX_CYCLE_STEPS[0],
        FIX_CYCLE_STEPS[1],
        Step(
            "2026-10-27T12:15:00+00:00",
            QueueItemStatus.BLOCKED_ON_USER,
            blocked_reason="review_pending_approval",
        ),
        Step("2026-10-27T12:35:00+00:00", _RUNNING),
        Step("2026-10-27T13:00:00+00:00", QueueItemStatus.COMPLETED),
    ]
    task, sessions = seed_fix_cycle(tmp_path, steps)

    history = _history(tmp_path, task, sessions)

    round_two = history.rounds[1]
    assert _parts(round_two.stage_seconds) == _expected(0.0, 1200.0, 600.0, 1200.0)
    assert round_two.wall_seconds == 3000.0
    assert _parts(history.summary.stage_seconds) == _expected(
        1800.0, 3600.0, 600.0, 1200.0
    )


def test_repair_starts_at_the_fix_session_not_at_the_handoff(tmp_path: Path) -> None:
    """The handoff-to-spawn gap (12:00 to 12:20) is reviewing, not repairing."""
    task, _ = seed_fix_cycle(tmp_path)
    late = fix_session_for(
        task,
        "sess-fix",
        "2026-10-27T12:20:00+00:00",
        FIX_SESSION_COMPLETED,
        worktree=derived_worktree(tmp_path),
    )

    history = _history(tmp_path, task, [late])

    assert _parts(history.rounds[1].stage_seconds) == _expected(
        270.0, 2430.0, 300.0, 0.0
    )


def test_an_impl_stage_segment_is_not_review_time(tmp_path: Path) -> None:
    steps = [
        Step("2026-10-27T11:00:00+00:00", _PENDING, Stage.IMPL),
        Step("2026-10-27T11:10:00+00:00", _RUNNING, Stage.IMPL),
        Step("2026-10-27T11:20:00+00:00", _PENDING),
        *FIX_CYCLE_STEPS[1:],
    ]
    task, sessions = seed_fix_cycle(tmp_path, steps)

    history = _history(tmp_path, task, sessions)

    assert _parts(history.summary.stage_seconds) == _expected(
        870.0, 4410.0, 1200.0, 3720.0
    )
    assert NOTE_HEAD_NOT_COVERED not in history.notes


def _three_round_ledger() -> ConvergenceLedger:
    """A dirty round, a clean loop round, then a clean codex confirm."""
    ledger = fresh_ledger(confirm="codex", ticket_id=ACME_TICKET)
    ledger, _ = record(
        ledger, round_finding(summary="a"), round_finding(summary="b"), sha="sha-0001"
    )
    ledger, _ = record(ledger, sha="sha-0002")
    ledger, _ = record(ledger, sha="sha-0002", kind="confirm")
    return timed(
        ledger,
        "2026-10-27T12:00:00+00:00",
        "2026-10-27T12:50:00+00:00",
        "2026-10-27T13:20:00+00:00",
    )


def test_a_pruned_head_nulls_the_summary_but_keeps_the_covered_rounds(
    tmp_path: Path,
) -> None:
    tail = [
        Step("2026-10-27T12:40:00+00:00", _PENDING),
        Step("2026-10-27T12:45:00+00:00", _RUNNING),
        Step("2026-10-27T13:30:00+00:00", QueueItemStatus.COMPLETED),
    ]
    task, sessions = seed_fix_cycle(tmp_path, tail, ledger=_three_round_ledger())

    history = _history(tmp_path, task, sessions)

    assert _parts(history.rounds[2].stage_seconds) == _expected(0.0, 1800.0, 0.0, 0.0)
    assert _parts(history.rounds[0].stage_seconds) == _expected(None, None, 0.0, None)
    assert _parts(history.summary.stage_seconds) == _expected(None, None, 1200.0, None)
    assert NOTE_HEAD_NOT_COVERED in history.notes
    assert note_uncovered("1, 2") in history.notes


def test_a_review_that_entered_without_a_pending_transition_nulls_the_summary(
    tmp_path: Path,
) -> None:
    """The first review-stage event is RUNNING, though round 1's window is covered."""
    task, sessions = seed_fix_cycle(tmp_path, FIX_CYCLE_STEPS[1:])

    history = _history(tmp_path, task, sessions)

    assert _parts(history.rounds[0].stage_seconds) == _expected(0.0, 600.0, 0.0, 0.0)
    assert _parts(history.rounds[1].stage_seconds) == _expected(
        270.0, 1530.0, 1200.0, 0.0
    )
    assert _parts(history.summary.stage_seconds) == _expected(None, None, 1200.0, None)
    assert history.notes.count(NOTE_HEAD_NOT_COVERED) == 1


def test_a_sentinel_only_review_gets_a_summary_and_default_round_blocks(
    tmp_path: Path,
) -> None:
    task = timing_task()
    status_chain(task, *FIX_CYCLE_STEPS)
    session = bound_session_for(
        task,
        worktree_path=derived_worktree(tmp_path),
        last_result=fixture_blocking_sentinel(),
    )

    history = _history(tmp_path, task, [session])

    assert history.sources[0] == "sentinel"
    assert [r.source for r in history.rounds] == ["sentinel_cycle0"]
    assert _parts(history.rounds[0].stage_seconds) == _ALL_NULL
    assert _parts(history.summary.stage_seconds) == _expected(
        2070.0, 5610.0, 0.0, 3720.0
    )
    assert _timing_notes(history) == []


def test_a_row_with_no_ledger_and_no_events_records_nothing_but_zero_repair(
    tmp_path: Path,
) -> None:
    history = history_for(timing_task(), tmp_path, now=NOW)

    assert _parts(history.summary.stage_seconds) == _expected(None, None, 0.0, None)
    assert history.summary.stage_seconds_as_of is None
    assert history.notes.count(NOTE_NO_TRANSITIONS) == 1
