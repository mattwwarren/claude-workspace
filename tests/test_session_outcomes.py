"""Tests for cw.session_outcomes: classifier, renderers and I/O wrapper (#2840).

Fixture inventory: autouse ``tmp_config_dir`` + ``_isolate_home`` only. ``_ev``
is local to this file. ``freeze_time`` and ``cw.events.record_event`` seed the
I/O section. ``tests._shared.session_seeding._make_daemon_session`` builds the
state sessions for ``live_ids_from_state``. ``tmp_events_dir`` and
``capture_events`` are not used.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time

from cw.events import record_event
from cw.history import EventType as HistoryEventType
from cw.history import HistoryEvent, append_event
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    CwState,
    OrchestratorEvent,
    OrchestratorEventType,
    SessionStatus,
)
from cw.session_outcomes import (
    NoResultCause,
    OutcomeReport,
    build_report,
    classify_sessions,
    format_report,
    live_ids_from_state,
    report_to_json,
)
from tests._shared.session_seeding import _make_daemon_session

T = OrchestratorEventType
C = NoResultCause
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
WS = T0 - timedelta(days=1)
WE = T0 + timedelta(days=1)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def _ev(
    etype: OrchestratorEventType, at: datetime, **payload: Any
) -> OrchestratorEvent:
    return OrchestratorEvent(type=etype, payload=payload, created_at=at)


def _spawn(
    sid: str, at: datetime, ticket: str = "T-1", client: str = "acme"
) -> OrchestratorEvent:
    return _ev(
        T.SESSION_SPAWNED,
        at,
        session_id=sid,
        ticket_id=ticket,
        client=client,
        lane="impl",
    )


def _classify(
    events: list[OrchestratorEvent],
    *,
    closed: frozenset[str] = frozenset(),
    live: frozenset[str] = frozenset(),
    samples: int = 3,
    window_start: datetime = WS,
) -> OutcomeReport:
    return classify_sessions(
        events,
        operator_closed_ids=closed,
        live_session_ids=live,
        window_start=window_start,
        window_end=WE,
        samples=samples,
    )


def _nonzero(report: OutcomeReport) -> dict[C, int]:
    return {b.cause: b.count for b in report.buckets if b.count}


# ---- Single-evidence buckets for one spawned session "s1" -----------------


def _s1(etype: OrchestratorEventType, **payload: Any) -> OrchestratorEvent:
    return _ev(etype, T0 + MIN, session_id="s1", **payload)


@pytest.mark.parametrize(
    ("expected", "extra"),
    [
        (C.USAGE_LIMIT, [_s1(T.SESSION_COMPLETED, reason="usage_limited_mid_turn")]),
        (C.USAGE_LIMIT, [_s1(T.SESSION_TIMED_OUT, cause="usage_limit_cutoff")]),
        (C.USAGE_LIMIT, [_s1(T.SESSION_REAP_PROPOSED, reason="usage_limit_cutoff")]),
        (C.USAGE_LIMIT, [_s1(T.SESSION_REAP_PROPOSED, reason="usage_limit_mid_turn")]),
        (
            C.USAGE_LIMIT,
            [_s1(T.SESSION_NEEDS_ATTENTION, paused_status="usage_limited_mid_turn")],
        ),
        (C.PHANTOM, [_s1(T.SESSION_PHANTOM_REVERTED, worktree_dirty=False)]),
        (C.PHANTOM, [_s1(T.SESSION_REAP_PROPOSED, reason="phantom_surface")]),
        (C.PHANTOM, [_s1(T.SESSION_COMPLETED, reason="phantom_reap_merged")]),
        (C.CRASH, [_s1(T.SESSION_COMPLETED, crashed=True)]),
        (
            C.CRASH,
            [_s1(T.SESSION_COMPLETED, crashed=True, reason="codex_orphaned_at_boot")],
        ),
        (C.REAP, [_s1(T.SESSION_REAP_PROPOSED, reason="idle_stall")]),
        (C.REAP, [_s1(T.SESSION_REAP_AUTHORIZED)]),
        (C.REAP, [_s1(T.SESSION_TIMED_OUT, cause="idle_stall_recovered")]),
        (C.PARKED_NEEDS_ATTENTION, [_s1(T.SESSION_NEEDS_ATTENTION, paused_status="x")]),
        (C.COMPLETED_NO_RESULT, [_s1(T.SESSION_COMPLETED, crashed=False)]),
        (C.UNCLASSIFIED, []),
    ],
)
def test_single_session_bucket(expected: C, extra: list[OrchestratorEvent]) -> None:
    report = _classify([_spawn("s1", T0), *extra])
    assert _nonzero(report) == {expected: 1}


def test_operator_close_bucket_from_closed_set() -> None:
    report = _classify([_spawn("s1", T0)], closed=frozenset({"s1"}))
    assert _nonzero(report) == {C.OPERATOR_CLOSE: 1}


def test_in_flight_when_live_and_no_evidence() -> None:
    report = _classify([_spawn("s1", T0)], live=frozenset({"s1"}))
    assert _nonzero(report) == {C.IN_FLIGHT: 1}


def test_live_session_with_reap_proposal_is_reap_not_in_flight() -> None:
    events = [_spawn("s1", T0), _s1(T.SESSION_REAP_PROPOSED, reason="idle_stall")]
    report = _classify(events, live=frozenset({"s1"}))
    assert _nonzero(report) == {C.REAP: 1}


# ---- Precedence --------------------------------------------------------------


def test_operator_close_beats_phantom() -> None:
    events = [_spawn("s1", T0), _s1(T.SESSION_PHANTOM_REVERTED)]
    assert _nonzero(_classify(events, closed=frozenset({"s1"}))) == {
        C.OPERATOR_CLOSE: 1
    }


def test_usage_limit_beats_phantom() -> None:
    events = [
        _spawn("s1", T0),
        _s1(T.SESSION_COMPLETED, reason="usage_limited_mid_turn"),
        _s1(T.SESSION_PHANTOM_REVERTED),
    ]
    assert _nonzero(_classify(events)) == {C.USAGE_LIMIT: 1}


def test_phantom_beats_crash() -> None:
    events = [
        _spawn("s1", T0),
        _s1(T.SESSION_COMPLETED, crashed=True),
        _s1(T.SESSION_PHANTOM_REVERTED),
    ]
    assert _nonzero(_classify(events)) == {C.PHANTOM: 1}


def test_needs_attention_plus_reap_is_reap() -> None:
    events = [
        _spawn("s1", T0),
        _s1(T.SESSION_NEEDS_ATTENTION, paused_status="x"),
        _s1(T.SESSION_REAP_PROPOSED, reason="idle_stall"),
    ]
    assert _nonzero(_classify(events)) == {C.REAP: 1}


# ---- Late-sentinel superseded (#2407) ----------------------------------------


def _handoff_events(
    *, a_result_at: datetime, extra: list[OrchestratorEvent], a_spawn: datetime = T0
) -> list[OrchestratorEvent]:
    """Predecessor a, released by a requeue, and successor s spawned at T0+2h."""
    return [
        _spawn("a", a_spawn),
        _ev(T.TICKET_REQUEUED, T0 + HOUR, ticket_id="T-1", client="acme"),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, a_result_at, session_id="a"),
        *extra,
    ]


def test_late_sentinel_superseded_after_requeue() -> None:
    report = _classify(_handoff_events(a_result_at=T0 + 3 * HOUR, extra=[]))
    assert report.spawned == 2
    assert report.with_result == 1
    assert _nonzero(report) == {C.LATE_SENTINEL_SUPERSEDED: 1}


def test_normal_handoff_result_before_successor_spawn_not_superseded() -> None:
    events = _handoff_events(a_result_at=T0 + HOUR + MIN, extra=[])
    assert _nonzero(_classify(events)) == {C.UNCLASSIFIED: 1}


def test_late_result_without_release_evidence_not_superseded() -> None:
    events = [
        _spawn("a", T0),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, T0 + 3 * HOUR, session_id="a"),
    ]
    assert _nonzero(_classify(events)) == {C.UNCLASSIFIED: 1}


def test_same_ticket_other_client_not_superseded() -> None:
    events = [
        _spawn("a", T0, client="other"),
        _ev(T.TICKET_REQUEUED, T0 + HOUR, ticket_id="T-1", client="other"),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, T0 + 3 * HOUR, session_id="a"),
    ]
    assert _nonzero(_classify(events)) == {C.UNCLASSIFIED: 1}


def test_predecessor_released_by_phantom_revert_is_superseded() -> None:
    events = [
        _spawn("a", T0),
        _ev(T.SESSION_PHANTOM_REVERTED, T0 + HOUR, session_id="a", ticket_id="T-1"),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, T0 + 3 * HOUR, session_id="a"),
    ]
    assert _nonzero(_classify(events)) == {C.LATE_SENTINEL_SUPERSEDED: 1}


def test_predecessor_in_operator_closed_set_is_superseded() -> None:
    events = [
        _spawn("a", T0),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, T0 + 3 * HOUR, session_id="a"),
    ]
    report = _classify(events, closed=frozenset({"a"}))
    assert _nonzero(report) == {C.LATE_SENTINEL_SUPERSEDED: 1}


def test_superseded_beats_operator_close() -> None:
    events = _handoff_events(a_result_at=T0 + 3 * HOUR, extra=[])
    report = _classify(events, closed=frozenset({"s"}))
    assert _nonzero(report) == {C.LATE_SENTINEL_SUPERSEDED: 1}


def test_predecessor_spawned_before_window_still_counts_for_superseded() -> None:
    before = WS - timedelta(days=1)
    events = [
        _spawn("a", before),
        _ev(T.TICKET_REQUEUED, T0 - HOUR, ticket_id="T-1", client="acme"),
        _spawn("s", T0),
        _ev(T.SESSION_RESULT_EMITTED, T0 + HOUR, session_id="a"),
    ]
    report = _classify(events)
    assert report.spawned == 1
    assert _nonzero(report) == {C.LATE_SENTINEL_SUPERSEDED: 1}


@pytest.mark.parametrize(
    ("requeued_at", "expected"),
    [
        pytest.param(T0 - HOUR, C.UNCLASSIFIED, id="before-predecessor-spawn"),
        pytest.param(T0, C.LATE_SENTINEL_SUPERSEDED, id="at-predecessor-spawn"),
    ],
)
def test_requeue_release_lower_bound_is_predecessor_spawn(
    requeued_at: datetime, expected: C
) -> None:
    """#3130: only a requeue at or after the predecessor's spawn releases it."""
    events = [
        _ev(T.TICKET_REQUEUED, requeued_at, ticket_id="T-1", client="acme"),
        _spawn("a", T0),
        _spawn("s", T0 + 2 * HOUR),
        _ev(T.SESSION_RESULT_EMITTED, T0 + 3 * HOUR, session_id="a"),
    ]
    assert _nonzero(_classify(events)) == {expected: 1}


def test_needs_attention_then_clean_completion_is_not_parked() -> None:
    """#3130: a terminal event after the park ends it; the session completed."""
    events = [
        _spawn("s1", T0),
        _s1(T.SESSION_NEEDS_ATTENTION, paused_status="x"),
        _ev(T.SESSION_COMPLETED, T0 + 2 * MIN, session_id="s1", crashed=False),
    ]
    assert _nonzero(_classify(events)) == {C.COMPLETED_NO_RESULT: 1}


# ---- live_ids_from_state (#3129) ---------------------------------------------


def test_live_ids_from_state_excludes_terminal_statuses() -> None:
    state = CwState(
        sessions=[
            _make_daemon_session(id=f"s-{status.value}", status=status)
            for status in SessionStatus
        ]
    )
    live = live_ids_from_state(state)
    expected = {
        f"s-{status.value}"
        for status in SessionStatus
        if status not in TERMINAL_SESSION_STATUSES
    }
    assert live == expected
    assert "s-completed" not in live
    assert "s-timed_out" not in live
    assert {"s-active", "s-idle", "s-backgrounded"} <= live


# ---- Window ------------------------------------------------------------------


def test_spawn_before_window_start_excluded_from_counts() -> None:
    events = [_spawn("s1", WS - MIN)]
    report = _classify(events)
    assert report.spawned == 0
    assert report.without_result == 0


def test_spawn_exactly_at_window_start_included() -> None:
    report = _classify([_spawn("s1", WS)])
    assert report.spawned == 1


# ---- Invariants and headline numbers -----------------------------------------


def test_invariant_buckets_sum_to_without_result() -> None:
    events = [
        _spawn("s1", T0),
        _spawn("s2", T0),
        _s1(T.SESSION_COMPLETED, crashed=True),
        _spawn("s3", T0),
        _ev(T.SESSION_RESULT_EMITTED, T0, session_id="s3"),
    ]
    report = _classify(events, live=frozenset({"s2"}))
    assert report.spawned == 3
    assert report.with_result == 1
    assert sum(b.count for b in report.buckets) == report.without_result == 2


def test_empty_report_has_zero_rate_and_no_truncation() -> None:
    report = _classify([])
    assert report.spawned == 0
    assert report.no_result_rate == 0.0
    assert report.failure_rate_excl_in_flight == 0.0
    assert report.largest_cause is None
    assert report.window_truncated is False


def test_all_with_result_gives_zero_buckets() -> None:
    events = [_spawn("s1", T0), _ev(T.SESSION_RESULT_EMITTED, T0, session_id="s1")]
    report = _classify(events)
    assert report.with_result == 1
    assert report.without_result == 0
    assert _nonzero(report) == {}


def test_largest_cause_excludes_in_flight() -> None:
    events = [_spawn("s1", T0), _spawn("s2", T0), _spawn("s3", T0)]
    events.append(_ev(T.SESSION_COMPLETED, T0 + MIN, session_id="s2", crashed=True))
    report = _classify(events, live=frozenset({"s1", "s3"}))
    assert report.count(C.IN_FLIGHT) == 2
    assert report.largest_cause is C.CRASH


def test_largest_cause_none_when_only_in_flight() -> None:
    report = _classify([_spawn("s1", T0)], live=frozenset({"s1"}))
    assert report.largest_cause is None


def test_largest_cause_tie_breaks_in_bucket_order() -> None:
    events = [
        _spawn("s1", T0),
        _spawn("s2", T0),
        _s1(T.SESSION_COMPLETED, crashed=True),
        _ev(T.SESSION_REAP_AUTHORIZED, T0 + MIN, session_id="s2"),
    ]
    assert _classify(events).largest_cause is C.CRASH


def test_failure_rate_excludes_in_flight() -> None:
    events = [_spawn("s1", T0), _spawn("s2", T0), _spawn("s3", T0)]
    events.append(_ev(T.SESSION_COMPLETED, T0 + MIN, session_id="s2", crashed=True))
    events.append(_ev(T.SESSION_RESULT_EMITTED, T0 + MIN, session_id="s3"))
    report = _classify(events, live=frozenset({"s1"}))
    assert report.no_result_rate == pytest.approx(2 / 3)
    assert report.failure_rate_excl_in_flight == pytest.approx(1 / 2)


def test_window_truncated_when_oldest_event_after_window_start() -> None:
    events = [_spawn("s1", T0)]
    assert _classify(events).window_truncated is True


def test_window_not_truncated_when_oldest_event_before_window_start() -> None:
    events = [_spawn("old", WS - MIN), _spawn("s1", T0)]
    assert _classify(events).window_truncated is False


def test_samples_capped_and_ordered_by_spawn_time() -> None:
    events = [
        _spawn("s2", T0 + MIN),
        _spawn("s1", T0),
        _spawn("s3", T0 + 2 * MIN),
    ]
    events.extend(
        _ev(T.SESSION_COMPLETED, T0 + 3 * MIN, session_id=sid, crashed=True)
        for sid in ("s1", "s2", "s3")
    )
    report = _classify(events, samples=2)
    crash = next(b for b in report.buckets if b.cause is C.CRASH)
    assert crash.count == 3
    assert crash.sample_session_ids == ("s1", "s2")


def test_samples_zero_returns_no_ids() -> None:
    events = [_spawn("s1", T0), _s1(T.SESSION_COMPLETED, crashed=True)]
    crash = next(b for b in _classify(events, samples=0).buckets if b.cause is C.CRASH)
    assert crash.count == 1
    assert crash.sample_session_ids == ()


# ---- Dedup and defensive parsing ---------------------------------------------


def test_duplicate_spawn_counts_once() -> None:
    report = _classify([_spawn("s1", T0), _spawn("s1", T0 + MIN)])
    assert report.spawned == 1


def test_non_string_session_id_and_missing_ticket_skipped() -> None:
    bad_sid = _ev(T.SESSION_SPAWNED, T0, session_id=123, ticket_id="T-1", client="acme")
    no_ticket = _ev(T.SESSION_SPAWNED, T0, session_id="s9", client="acme")
    report = _classify([bad_sid, no_ticket])
    assert report.spawned == 0


def test_result_wins_over_crashed_completion() -> None:
    events = [
        _spawn("s1", T0),
        _s1(T.SESSION_COMPLETED, crashed=True),
        _ev(T.SESSION_RESULT_EMITTED, T0 + MIN, session_id="s1"),
    ]
    report = _classify(events)
    assert report.with_result == 1
    assert report.without_result == 0


# ---- Renderers ---------------------------------------------------------------


def test_format_zero_spawn_message() -> None:
    text = format_report(_classify([]))
    assert "no worker sessions spawned in window" in text


def test_format_normal_has_headline_row_and_largest_line() -> None:
    events = [_spawn("s1", T0), _s1(T.SESSION_COMPLETED, crashed=True)]
    text = format_report(_classify(events))
    assert "Worker sessions spawned: 1" in text
    assert "Largest failure cause: crash (1)" in text
    assert "crash" in text
    assert "s1" in text


def test_format_truncated_note() -> None:
    text = format_report(_classify([_spawn("s1", T0)]))
    assert "window starts before the oldest inbox event" in text


def test_report_to_json_keys_and_round_trip() -> None:
    events = [_spawn("s1", T0), _s1(T.SESSION_COMPLETED, crashed=True)]
    payload = report_to_json(_classify(events))
    assert set(payload) == {
        "schema_version",
        "window",
        "clients",
        "spawned",
        "with_result",
        "without_result",
        "no_result_rate",
        "failure_rate_excl_in_flight",
        "largest_cause",
        "oldest_event_at",
        "window_truncated",
        "buckets",
    }
    assert payload["largest_cause"] == "crash"
    buckets = payload["buckets"]
    assert isinstance(buckets, list)
    assert len(buckets) == len(C)
    json.dumps(payload)


# ---- I/O wrapper (build_report) ----------------------------------------------


def _record_at(at: datetime, etype: T, **payload: Any) -> None:
    with freeze_time(at):
        record_event(etype, payload)


def test_build_report_counts_seeded_inbox() -> None:
    _record_at(T0, T.SESSION_SPAWNED, session_id="s1", ticket_id="T-1", client="acme")
    _record_at(T0, T.SESSION_SPAWNED, session_id="s2", ticket_id="T-2", client="acme")
    _record_at(T0 + MIN, T.SESSION_RESULT_EMITTED, session_id="s1", client="acme")
    _record_at(T0 + MIN, T.SESSION_COMPLETED, session_id="s2", crashed=True)
    report = build_report(
        days=1,
        now=T0 + HOUR,
        client_names=None,
        live_session_ids=frozenset(),
        samples=3,
    )
    assert report.spawned == 2
    assert report.with_result == 1
    assert report.clients == ("acme",)
    assert report.largest_cause is C.CRASH


def test_build_report_finds_operator_close_from_history() -> None:
    _record_at(T0, T.SESSION_SPAWNED, session_id="s1", ticket_id="T-1", client="acme")
    append_event(
        "acme",
        HistoryEvent(
            timestamp=T0 + MIN,
            event_type=HistoryEventType.SESSION_COMPLETED,
            client="acme",
            session_id="s1",
            detail="operator_close",
            metadata={"session_id": "s1"},
        ),
    )
    report = build_report(
        days=1,
        now=T0 + HOUR,
        client_names=None,
        live_session_ids=frozenset(),
        samples=3,
    )
    assert report.largest_cause is C.OPERATOR_CLOSE


def test_history_line_with_other_detail_is_not_operator_close() -> None:
    _record_at(T0, T.SESSION_SPAWNED, session_id="s1", ticket_id="T-1", client="acme")
    append_event(
        "acme",
        HistoryEvent(
            timestamp=T0 + MIN,
            event_type=HistoryEventType.SESSION_COMPLETED,
            client="acme",
            session_id="s1",
            detail="something_else",
            metadata={"session_id": "s1"},
        ),
    )
    report = build_report(
        days=1,
        now=T0 + HOUR,
        client_names=None,
        live_session_ids=frozenset(),
        samples=3,
    )
    assert report.largest_cause is C.UNCLASSIFIED


def test_client_filter_limits_spawns() -> None:
    _record_at(T0, T.SESSION_SPAWNED, session_id="s1", ticket_id="T-1", client="acme")
    _record_at(T0, T.SESSION_SPAWNED, session_id="s2", ticket_id="T-2", client="other")
    report = build_report(
        days=1,
        now=T0 + HOUR,
        client_names=frozenset({"acme"}),
        live_session_ids=frozenset(),
        samples=3,
    )
    assert report.spawned == 1
    assert report.clients == ("acme",)


def test_build_report_empty_inbox() -> None:
    report = build_report(
        days=7,
        now=T0,
        client_names=None,
        live_session_ids=frozenset(),
        samples=3,
    )
    assert report.spawned == 0


def test_build_report_does_not_swallow_read_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> list[OrchestratorEvent]:
        msg = "unreadable"
        raise OSError(msg)

    monkeypatch.setattr("cw.session_outcomes.read_events", _boom)
    with pytest.raises(OSError, match="unreadable"):
        build_report(
            days=1,
            now=T0,
            client_names=None,
            live_session_ids=frozenset(),
            samples=3,
        )
