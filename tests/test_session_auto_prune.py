"""Tests for cw.session_auto_prune — automatic bounded session pruning (#2965)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from cw.config import load_state, sessions_lock, state_dir, state_file
from cw.dev_queue import save_dev_queue
from cw.events import read_events
from cw.exceptions import (
    CwLockOrderError,
    CwLockReentrancyError,
    SessionsLockTimeoutError,
)
from cw.models import (
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionStatus,
)
from cw.session_auto_prune import (
    AutoPruneStatus,
    auto_prune_sessions,
    read_auto_prune_status,
)
from cw.session_retention import _SESSION_RETENTION_DAYS
from tests._shared.session_seeding import (
    _make_daemon_session,
    _make_ticket_task,
    _seed_sessions,
)

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_EVENT_PAYLOAD_KEYS = {
    "archived_count",
    "deleted_count",
    "kept_count",
    "cutoff",
    "age_days",
    "max_per_run",
    "capped",
    "bytes_before",
    "bytes_after",
    "archive_path",
}


def _sidecar_path() -> Path:
    return state_dir() / "sessions_auto_prune.json"


def _terminal(session_id: str, age_days: int, *, name: str | None = None) -> Session:
    """A COMPLETED session whose age anchor is *age_days* before ``_NOW``."""
    stamp = _NOW - timedelta(days=age_days)
    return _make_daemon_session(
        id=session_id,
        name=name or f"client-a/auto-dev/T-{session_id}",
        client="client-a",
        status=SessionStatus.COMPLETED,
        completed_at=stamp,
        started_at=stamp,
    )


def _prune_events() -> list[object]:
    return list(read_events(event_types=[OrchestratorEventType.SESSIONS_AUTO_PRUNED]))


def _status() -> AutoPruneStatus:
    status = read_auto_prune_status()
    assert status is not None
    return status


def _lock_violation(kind: type[Exception]) -> Exception:
    path = Path("/tmp/sessions.lock")
    if kind is CwLockReentrancyError:
        return CwLockReentrancyError(
            "reentry", lock_name="sessions", path=path, held=("sessions",)
        )
    return CwLockOrderError(
        "order",
        lock_name="sessions",
        path=path,
        rank_name="SESSIONS",
        held=("leaf",),
    )


class TestAutoPruneSessions:
    def test_disabled_is_noop(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))

        def _must_not_run(**_kw: object) -> None:
            msg = "prune_sessions must not run when disabled"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.session_auto_prune.prune_sessions", _must_not_run)
        config = OrchestratorConfig(sessions_auto_prune_enabled=False)

        with freeze_time(_NOW):
            assert auto_prune_sessions(config) is None

        assert not _sidecar_path().exists()
        assert _prune_events() == []
        assert len(load_state().sessions) == 1

    def test_first_run_prunes_terminal_old_and_writes_sidecar_and_event(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(
            _terminal("old00001", 60),
            _terminal("old00002", 45),
            _terminal("new00001", 2),
            _make_daemon_session(
                id="live0001",
                name="client-a/auto-dev/T-live",
                status=SessionStatus.ACTIVE,
                started_at=_NOW - timedelta(days=90),
            ),
        )
        config = OrchestratorConfig()

        with freeze_time(_NOW):
            result = auto_prune_sessions(config)

        assert result is not None
        assert result.archived_count == 2
        assert {s.id for s in load_state().sessions} == {"new00001", "live0001"}
        events = read_events(event_types=[OrchestratorEventType.SESSIONS_AUTO_PRUNED])
        assert len(events) == 1
        event = events[0]
        assert event.correlation_id is None
        assert set(event.payload) == _EVENT_PAYLOAD_KEYS
        assert event.payload["archived_count"] == 2
        assert event.payload["kept_count"] == 2
        assert event.payload["age_days"] == config.sessions_auto_prune_age_days
        assert event.payload["max_per_run"] == config.sessions_auto_prune_max_per_run
        assert event.payload["capped"] is False
        assert event.payload["bytes_before"] > event.payload["bytes_after"]
        expected_cutoff = _NOW - timedelta(days=config.sessions_auto_prune_age_days)
        assert event.payload["cutoff"] == expected_cutoff.isoformat()
        status = _status()
        assert status.status == "ok"
        assert status.archived_count == 2
        assert status.last_run_at == _NOW

    def test_age_and_cap_come_from_config_and_drain_over_runs(
        self, tmp_config_dir: Path
    ) -> None:
        ages = {"ses00001": 20, "ses00002": 50, "ses00003": 30, "ses00004": 40}
        _seed_sessions(
            *[_terminal(sid, age) for sid, age in ages.items()],
            _terminal("young001", 3),
        )
        config = OrchestratorConfig(
            sessions_auto_prune_age_days=7,
            sessions_auto_prune_max_per_run=2,
            sessions_auto_prune_interval_minutes=60,
        )

        with freeze_time(_NOW):
            first = auto_prune_sessions(config)
        assert first is not None
        assert first.capped is True
        assert first.cutoff == _NOW - timedelta(days=7)
        assert {s.id for s in load_state().sessions} == {
            "ses00001",
            "ses00003",
            "young001",
        }
        assert _status().capped is True

        with freeze_time(_NOW + timedelta(minutes=61)):
            second = auto_prune_sessions(config)
        assert second is not None
        assert second.archived_count == 2
        assert second.capped is False
        assert [s.id for s in load_state().sessions] == ["young001"]

    def test_interval_gate_skips_second_call_within_interval(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))
        config = OrchestratorConfig(sessions_auto_prune_interval_minutes=60)

        with freeze_time(_NOW):
            assert auto_prune_sessions(config) is not None
        _seed_sessions(_terminal("ses00002", 60))
        with freeze_time(_NOW + timedelta(minutes=59)):
            assert auto_prune_sessions(config) is None

        assert [s.id for s in load_state().sessions] == ["ses00002"]
        assert len(_prune_events()) == 1
        assert _status().last_run_at == _NOW

    def test_runs_again_after_interval(self, tmp_config_dir: Path) -> None:
        _seed_sessions(_terminal("ses00001", 60))
        config = OrchestratorConfig(sessions_auto_prune_interval_minutes=60)

        with freeze_time(_NOW):
            assert auto_prune_sessions(config) is not None
        _seed_sessions(_terminal("ses00002", 60))
        later = _NOW + timedelta(minutes=60)
        with freeze_time(later):
            result = auto_prune_sessions(config)

        assert result is not None
        assert result.archived_count == 1
        assert load_state().sessions == []
        assert _status().last_run_at == later

    def test_no_candidates_updates_sidecar_but_emits_no_event(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(_terminal("new00001", 2))
        before = state_file().read_bytes()

        with freeze_time(_NOW):
            result = auto_prune_sessions(OrchestratorConfig())

        assert result is not None
        assert result.archived_count == 0
        assert _prune_events() == []
        assert state_file().read_bytes() == before
        status = _status()
        assert status.status == "ok"
        assert status.archived_count == 0
        assert status.last_run_at == _NOW

    def test_never_touches_nonterminal_bound_or_recent(
        self, tmp_config_dir: Path
    ) -> None:
        old = _NOW - timedelta(days=90)
        rows = {
            "pend0001": QueueItemStatus.PENDING,
            "runn0001": QueueItemStatus.RUNNING,
            "done0001": QueueItemStatus.COMPLETED,
            "blck0001": QueueItemStatus.BLOCKED_ON_USER,
        }
        _seed_sessions(
            _make_daemon_session(
                id="act00001",
                name="client-a/auto-dev/T-act",
                status=SessionStatus.ACTIVE,
                started_at=old,
            ),
            _make_daemon_session(
                id="idle0001",
                name="client-a/auto-dev/T-idle",
                status=SessionStatus.IDLE,
                started_at=old,
            ),
            *[_terminal(sid, 90) for sid in rows],
            _terminal("byid0001", 90, name="client-a/impl"),
            _terminal("recent01", 2),
            _terminal("prune001", 90),
        )
        tasks = [
            _make_ticket_task(
                ticket_id=f"T-{sid}", client="client-a", status=row_status
            )
            for sid, row_status in rows.items()
        ]
        tasks.append(
            _make_ticket_task(
                ticket_id="T-elsewhere", client="client-b", session_id="byid0001"
            )
        )
        save_dev_queue(DevQueueStore(tasks=tasks))

        with freeze_time(_NOW):
            result = auto_prune_sessions(OrchestratorConfig())

        assert result is not None
        assert result.archived_count == 1
        remaining = {s.id for s in load_state().sessions}
        assert "prune001" not in remaining
        assert remaining == {
            "act00001",
            "idle0001",
            *rows,
            "byid0001",
            "recent01",
        }

    def test_lock_timeout_is_recorded_not_raised(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))
        before = state_file().read_bytes()

        def _timeout(**_kw: object) -> None:
            msg = "timed out"
            raise SessionsLockTimeoutError(
                msg,
                lock_path=Path("/tmp/sessions.lock"),
                waited_s=60.0,
            )

        monkeypatch.setattr("cw.session_retention.sessions_lock", _timeout)

        with freeze_time(_NOW):
            assert auto_prune_sessions(OrchestratorConfig()) is None

        status = _status()
        assert status.status == "failed"
        assert status.error
        assert _prune_events() == []
        assert state_file().read_bytes() == before

    def test_midway_save_failure_leaves_sessions_json_valid_and_records_failure(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60), _terminal("ses00002", 70))
        before = state_file().read_bytes()

        def _boom(_state: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.session_retention.save_state", _boom)

        with freeze_time(_NOW):
            assert auto_prune_sessions(OrchestratorConfig()) is None

        status = _status()
        assert status.status == "failed"
        assert status.error is not None
        assert "disk full" in status.error
        assert state_file().read_bytes() == before
        assert len(load_state().sessions) == 2
        assert _prune_events() == []

    def test_malformed_sessions_json_records_failure(
        self, tmp_config_dir: Path
    ) -> None:
        state_file().write_text("{not json")

        with freeze_time(_NOW):
            assert auto_prune_sessions(OrchestratorConfig()) is None

        assert _status().status == "failed"
        assert state_file().read_text() == "{not json"
        assert _prune_events() == []

    def test_event_emit_failure_does_not_lose_sidecar(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))

        def _boom(*_a: object, **_k: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr("cw.session_auto_prune.record_event", _boom)

        with freeze_time(_NOW), caplog.at_level("WARNING"):
            result = auto_prune_sessions(OrchestratorConfig())

        assert result is not None
        assert _status().status == "ok"
        assert any("sessions.auto_pruned" in r.getMessage() for r in caplog.records)

    def test_runs_with_no_sessions_lock_held_by_caller(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))
        with freeze_time(_NOW):
            result = auto_prune_sessions(OrchestratorConfig())
        assert result is not None
        assert result.archived_count == 1

    @pytest.mark.lock_violations_expected("reentry")
    def test_reentry_inside_sessions_lock_is_reraised_not_recorded(
        self, tmp_config_dir: Path
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))

        with (
            freeze_time(_NOW),
            sessions_lock(),
            pytest.raises(CwLockReentrancyError),
        ):
            auto_prune_sessions(OrchestratorConfig())

        assert not _sidecar_path().exists()
        assert _prune_events() == []

    @pytest.mark.parametrize("kind", [CwLockReentrancyError, CwLockOrderError])
    def test_lock_discipline_errors_are_reraised_not_recorded(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: type[Exception],
    ) -> None:
        error = _lock_violation(kind)

        def _raise(**_kw: object) -> None:
            raise error

        monkeypatch.setattr("cw.session_auto_prune.prune_sessions", _raise)

        with freeze_time(_NOW), pytest.raises(kind):
            auto_prune_sessions(OrchestratorConfig())

        assert not _sidecar_path().exists()
        assert _prune_events() == []

    def test_future_last_run_at_counts_as_due(self, tmp_config_dir: Path) -> None:
        """Clock skew or a restored sidecar must not stall the schedule."""
        _seed_sessions(_terminal("ses00001", 60))
        status = AutoPruneStatus(
            last_run_at=_NOW + timedelta(days=30),
            status="ok",
            age_days=30,
            max_per_run=500,
        )
        _sidecar_path().write_text(status.model_dump_json())

        with freeze_time(_NOW):
            result = auto_prune_sessions(OrchestratorConfig())

        assert result is not None
        assert result.archived_count == 1
        assert _status().last_run_at == _NOW

    def test_sidecar_write_failure_is_logged_not_raised(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed_sessions(_terminal("ses00001", 60))

        def _boom(_path: Path, _text: str) -> None:
            msg = "read-only state dir"
            raise OSError(msg)

        monkeypatch.setattr("cw.sidecar_status.atomic_write_text", _boom)

        with freeze_time(_NOW), caplog.at_level("WARNING"):
            result = auto_prune_sessions(OrchestratorConfig())

        assert result is not None
        assert result.archived_count == 1
        assert read_auto_prune_status() is None
        assert any("sessions_auto_prune.json" in r.getMessage() for r in caplog.records)

    def test_age_default_matches_manual_retention_constant(self) -> None:
        assert (
            OrchestratorConfig().sessions_auto_prune_age_days == _SESSION_RETENTION_DAYS
        )


class TestReadAutoPruneStatus:
    def test_read_status_absent_corrupt_and_valid(self, tmp_config_dir: Path) -> None:
        assert read_auto_prune_status() is None

        _sidecar_path().write_text("{not json")
        assert read_auto_prune_status() is None

        _sidecar_path().write_text(json.dumps([1, 2, 3]))
        assert read_auto_prune_status() is None

        status = AutoPruneStatus(
            last_run_at=_NOW,
            status="ok",
            archived_count=3,
            age_days=30,
            max_per_run=500,
        )
        _sidecar_path().write_text(status.model_dump_json())
        assert read_auto_prune_status() == status
