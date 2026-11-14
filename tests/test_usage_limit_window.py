"""Tests for cw.usage_limit_window: the inspect view and the operator clear (#3547)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.dev_queue import list_tickets, save_dev_queue
from cw.dispatch_state import (
    load_usage_limit_armed_at,
    load_usage_limited_until,
    record_usage_limit_armed,
    save_usage_limit_armed_at,
)
from cw.dispatch_state_marks import load_usage_limit_clear_marks
from cw.events import read_events, record_event
from cw.exceptions import DEV_QUEUE_LOCK_NAME, CwError
from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask
from cw.models.gate_block import GateBlockBackoff
from cw.usage_limit_window import (
    ClearPlan,
    ClearResult,
    StampedRow,
    UsageLimitWindowView,
    apply_usage_limit_clear,
    plan_usage_limit_clear,
    render_usage_limit_lines,
    usage_limit_window_views,
)
from tests._shared.dispatch_fixtures import seed_stamped_row, seed_usage_limit_window
from tests._shared.lock_helpers import (
    DISPATCH_STATE_LOCK_NAME,
    record_state_locks,
    spy_no_locks_held,
)

_ACME = "acme"
_NOW = datetime.now(UTC).replace(microsecond=0)
_UNTIL = _NOW + timedelta(hours=2)
_STAMP = _NOW + timedelta(hours=1)


def _cleared_events() -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(event_types=[OrchestratorEventType.USAGE_LIMIT_CLEARED])
    ]


def _row(ticket_id: str) -> TicketTask:
    return next(t for t in list_tickets(_ACME) if t.ticket_id == ticket_id)


def _apply(plan: ClearPlan | None = None) -> ClearResult:
    return apply_usage_limit_clear(
        plan if plan is not None else plan_usage_limit_clear(_ACME),
        actor="operator-login",
        forced=False,
        probe_verdict="available",
    )


class TestViews:
    def test_active_windows_only_sorted_by_client(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window("zeta", _UNTIL)
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_usage_limit_window("lapsed", _NOW - timedelta(minutes=1))

        assert [v.client for v in usage_limit_window_views()] == [_ACME, "zeta"]

    def test_no_window_reads_no_events(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing active, neither the arm marker nor the event log is read."""
        reads: list[object] = []
        monkeypatch.setattr(
            "cw.usage_limit_window.read_events",
            lambda **kwargs: reads.append(kwargs) or [],
        )

        assert usage_limit_window_views() == []
        assert reads == []

    def test_client_filter(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window("zeta", _UNTIL)
        seed_usage_limit_window(_ACME, _UNTIL)

        assert [v.client for v in usage_limit_window_views("zeta")] == ["zeta"]

    def test_armed_at_is_the_fleet_level_value(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window("zeta", _UNTIL)
        seed_usage_limit_window(_ACME, _UNTIL)
        save_usage_limit_armed_at(_NOW)

        views = usage_limit_window_views()

        assert {v.armed_at for v in views} == {_NOW}

    def test_armed_at_none_when_not_persisted(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)

        assert usage_limit_window_views()[0].armed_at is None

    def test_source_from_latest_matching_armed_event(
        self, tmp_config_dir: Path
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_usage_limit_window("zeta", _UNTIL)
        record_usage_limit_armed(_ACME, until=_UNTIL, reset_at=None)
        record_usage_limit_armed(_ACME, until=_UNTIL, reset_at=_UNTIL)
        record_usage_limit_armed("zeta", until=_STAMP, reset_at=None)

        by_client = {v.client: v.source for v in usage_limit_window_views()}

        assert by_client == {_ACME: "parsed_reset", "zeta": None}

    def test_reads_through_the_loops_reader_once(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        calls: list[int] = []

        def _counting() -> dict[str, datetime]:
            calls.append(1)
            return load_usage_limited_until()

        monkeypatch.setattr("cw.usage_limit_window.load_usage_limited_until", _counting)

        usage_limit_window_views()

        assert calls == [1]

    def test_to_json_and_render(self) -> None:
        view = UsageLimitWindowView(
            client=_ACME,
            until=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            armed_at=None,
            source="flat_backoff",
        )

        assert view.to_json() == {
            "client": _ACME,
            "until": "2026-11-01T12:00:00Z",
            "armed_at": None,
            "source": "flat_backoff",
        }
        assert render_usage_limit_lines([view]) == [
            "Usage-limit windows:",
            "  acme: until 2026-11-01T12:00:00Z (armed-at none, fleet-level;"
            " source flat_backoff)",
        ]
        assert render_usage_limit_lines([]) == []


class TestPlan:
    def test_window_only(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)

        plan = plan_usage_limit_clear(_ACME)

        assert plan.window_until == _UNTIL
        assert plan.rows == ()
        assert not plan.is_empty

    def test_rows_only(self, tmp_config_dir: Path) -> None:
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)

        plan = plan_usage_limit_clear(_ACME)

        assert plan.window_until is None
        assert plan.rows == (StampedRow("A-1", _STAMP, None),)
        assert not plan.is_empty

    def test_neither_is_empty(self, tmp_config_dir: Path) -> None:
        assert plan_usage_limit_clear(_ACME).is_empty

    def test_only_future_stamped_pending_rows_of_the_client(
        self, tmp_config_dir: Path
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        seed_stamped_row(
            _ACME,
            "A-RUN",
            next_eligible_at=_STAMP,
            status=QueueItemStatus.RUNNING,
            session_id="s-1",
        )
        seed_stamped_row(
            _ACME,
            "A-BLK",
            next_eligible_at=_STAMP,
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id="s-2",
        )
        seed_stamped_row(_ACME, "A-OLD", next_eligible_at=_NOW - timedelta(hours=1))
        seed_stamped_row("other", "O-1", next_eligible_at=_STAMP)

        plan = plan_usage_limit_clear(_ACME)

        assert [r.ticket_id for r in plan.rows] == ["A-1"]

    def test_gate_owned_row_is_excluded(self, tmp_config_dir: Path) -> None:
        seed_stamped_row(
            _ACME,
            "A-GATE",
            next_eligible_at=_STAMP,
            gate_block=GateBlockBackoff(count=1, fingerprint="f", until=_STAMP),
        )

        assert plan_usage_limit_clear(_ACME).is_empty

    def test_layered_gate_wait_is_restored(self, tmp_config_dir: Path) -> None:
        gate_until = _NOW + timedelta(minutes=10)
        seed_stamped_row(
            _ACME,
            "A-LAYER",
            next_eligible_at=_STAMP,
            gate_block=GateBlockBackoff(count=1, fingerprint="f", until=gate_until),
        )

        plan = plan_usage_limit_clear(_ACME)

        assert plan.rows == (StampedRow("A-LAYER", _STAMP, gate_until),)


class TestApply:
    def test_removes_window_writes_mark_and_releases_rows(
        self, tmp_config_dir: Path
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        gate = GateBlockBackoff(count=2, fingerprint="f", until=_NOW)
        seed_stamped_row(
            _ACME,
            "A-1",
            next_eligible_at=_STAMP,
            spawn_error_count=3,
            attempts=2,
            gate_block=gate,
        )

        result = _apply()

        assert load_usage_limited_until() == {}
        assert _ACME in load_usage_limit_clear_marks()
        row = _row("A-1")
        assert row.next_eligible_at is None
        assert row.spawn_error_count == 3
        assert row.attempts == 2
        assert row.gate_block == gate
        assert result.rows_released == ("A-1",)
        assert result.old_until == _UNTIL

    def test_idempotent(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        _apply()

        assert plan_usage_limit_clear(_ACME).is_empty
        result = _apply()

        assert result.old_until is None
        assert result.rows_released == ()

    def test_records_one_operator_cleared_event(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        save_usage_limit_armed_at(_NOW)
        saves: list[object] = []
        monkeypatch.setattr("cw.dispatch_state.save_usage_limit_armed_at", saves.append)

        _apply()

        events = _cleared_events()
        assert len(events) == 1
        event = events[0]
        assert event["clients_affected"] == [_ACME]
        assert event["sessions_affected"] == 0
        assert event["detected_at"] == _NOW.isoformat()
        assert isinstance(event["cleared_at"], str)
        assert event["trigger"] == "operator"
        assert event["cleared_by"] == "operator-login"
        assert event["old_until"] == _UNTIL.isoformat()
        assert event["rows_released"] == ["A-1"]
        assert event["forced"] is False
        assert event["probe"] == "available"
        assert saves == []
        assert load_usage_limit_armed_at() == _NOW

    def test_detected_at_null_without_armed_at(self, tmp_config_dir: Path) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)

        _apply()

        assert _cleared_events()[0]["detected_at"] is None
        assert _cleared_events()[0]["old_until"] == _UNTIL.isoformat()

    def test_locks_are_taken_once_each_and_never_nested(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        plan = plan_usage_limit_clear(_ACME)
        snapshots = spy_no_locks_held(
            monkeypatch, "cw.usage_limit_window.record_event", returns=None
        )

        with record_state_locks() as recorder:
            _apply(plan)

        assert recorder.acquires(DISPATCH_STATE_LOCK_NAME) == 1
        assert recorder.acquires(DEV_QUEUE_LOCK_NAME) == 1
        assert not recorder.acquired_while_held(
            DEV_QUEUE_LOCK_NAME, DISPATCH_STATE_LOCK_NAME
        )
        assert not recorder.acquired_while_held(
            DISPATCH_STATE_LOCK_NAME, DEV_QUEUE_LOCK_NAME
        )
        assert snapshots == [()]

    def test_audit_event_is_written_after_state_changes(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        seen_at_write: list[dict[str, object]] = []

        def _spy(event_type: OrchestratorEventType, payload: dict[str, object]) -> None:
            seen_at_write.append(
                {
                    "window": load_usage_limited_until(),
                    "row": _row("A-1").next_eligible_at,
                    "marks": load_usage_limit_clear_marks(),
                }
            )
            record_event(event_type, payload)

        monkeypatch.setattr("cw.usage_limit_window.record_event", _spy)

        _apply()

        assert seen_at_write == [
            {
                "window": {},
                "row": None,
                "marks": {_ACME: next(iter(load_usage_limit_clear_marks().values()))},
            }
        ]
        assert len(_cleared_events()) == 1

    def test_event_write_failure_reports_post_clear_state(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "events disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.usage_limit_window.record_event", _boom)

        with (
            caplog.at_level(logging.ERROR, logger="cw.usage_limit_window"),
            pytest.raises(CwError) as excinfo,
        ):
            _apply()

        message = str(excinfo.value)
        assert f"cleared usage-limit window for {_ACME}" in message
        assert (
            "the dispatch.usage_limit_cleared audit event could not be recorded"
            in message
        )
        assert "events disk full" in message
        assert load_usage_limited_until() == {}
        assert _ACME in load_usage_limit_clear_marks()
        assert _row("A-1").next_eligible_at is None
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(_ACME in m and "could not be recorded" in m for m in errors)
        assert plan_usage_limit_clear(_ACME).is_empty

    def test_row_save_failure_still_records_the_event(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)
        seed_stamped_row(_ACME, "A-2", next_eligible_at=_STAMP)

        def _boom(_store: object) -> None:
            msg = "queue disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.usage_limit_window.save_dev_queue", _boom)

        with pytest.raises(CwError, match="queue disk full"):
            _apply()

        events = _cleared_events()
        assert len(events) == 1
        assert events[0]["rows_released"] == []
        assert events[0]["old_until"] == _UNTIL.isoformat()
        assert load_usage_limited_until() == {}
        monkeypatch.setattr("cw.usage_limit_window.save_dev_queue", save_dev_queue)

        plan = plan_usage_limit_clear(_ACME)
        assert plan.window_until is None
        assert [r.ticket_id for r in plan.rows] == ["A-1", "A-2"]
        assert _apply(plan).rows_released == ("A-1", "A-2")

    def test_window_clear_failure_does_not_record_event(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window(_ACME, _UNTIL)
        seed_stamped_row(_ACME, "A-1", next_eligible_at=_STAMP)

        def _boom(_client: str, *, cleared_at: datetime, **_kwargs: object) -> None:
            msg = "state disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.usage_limit_window.clear_usage_limited_until", _boom)

        with pytest.raises(
            CwError,
            match=f"could not clear the usage-limit window for {_ACME}",
        ):
            _apply()

        assert load_usage_limited_until() == {_ACME: _UNTIL}
        assert _row("A-1").next_eligible_at == _STAMP
        assert len(_cleared_events()) == 0
