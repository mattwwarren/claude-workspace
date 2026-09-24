"""Unit tests for cw.reconcile.usage_limit_mid_turn (#2324).

A still-roster-present worker whose transcript tail is a usage-limit message
with no sentinel is detected, the client's ``usage_limited_until`` lockout is
armed, and the dev-queue row is dispositioned without an unproductive-attempt
charge -- RUNNING->PENDING (+ ``next_eligible_at``) under ``reap_policy: auto``,
RUNNING->BLOCKED_ON_USER under ``signal_only``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import freezegun
import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dispatch_state import (
    load_usage_limited_until,
    merge_and_save_usage_limited_until,
)
from cw.events import read_events, record_event
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    ProposedAction,
    _deps,
    detect_and_park_mid_turn_usage_limits,
)
from cw.reconcile.usage_limit_mid_turn import (
    _act_on_mid_turn_usage_limit_candidates,
    _detect_mid_turn_usage_limit_candidates,
)
from tests._reconcile_helpers import (
    _auto_config,
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
    _ul_record,
    _write_transcript_records,
)
from tests.conftest import _make_ticket_task

# Verbatim from the #2324 ticket: the text all three stalled workers ended on.
_LIMIT_TEXT = "You've hit your weekly limit · resets Sep 26, 11pm (America/New_York)"
_UNPARSEABLE_LIMIT_TEXT = "You've hit your weekly limit · resets soon"

_NY = ZoneInfo("America/New_York")
_SID = "2324-mid"
_SURFACE = "fake-short-id"
_CLIENT = "client-a"
_STARTED_AT = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)
_RESET_AT = datetime(2026, 9, 26, 23, 0, tzinfo=_NY).astimezone(UTC)
_BACKOFF_SECONDS = 1800

_T_USER = "2026-09-24T00:10:00+00:00"
_T_WORK = "2026-09-24T00:20:00+00:00"
_T_LIMIT = "2026-09-24T00:50:00+00:00"
_T_AFTER = "2026-09-24T00:55:00+00:00"


def _user_record(text: str, timestamp: str) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _trailing_metadata() -> list[dict[str, object]]:
    """The non-content records observed after a real mid-turn limit stop.

    Session 5f9ed4a6 (one of #2324's three incidents) ends on the limit text,
    then ``cost-state`` and ``last-prompt`` -- no literal ``turn_duration``.
    Only the record ``type`` values were observed; the other fields are filler
    that nothing under test reads.
    """
    return [
        {"type": "cost-state", "timestamp": _T_AFTER, "costUSD": 1.23},
        {"type": "last-prompt", "timestamp": _T_AFTER, "prompt": "/auto-dev"},
    ]


def _limit_record(text: str = _LIMIT_TEXT) -> dict[str, object]:
    """The limit record, carrying the error fields seen on the real capture."""
    return {**_ul_record(text, _T_LIMIT), "error": "rate_limit", "apiErrorStatus": 429}


def _limit_tail(text: str = _LIMIT_TEXT) -> list[dict[str, object]]:
    return [
        _user_record("implement the plan", _T_USER),
        _ul_record("working on it", _T_WORK),
        _limit_record(text),
        *_trailing_metadata(),
    ]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setattr("cw.reconcile._deps.host_timezone", lambda: _NY)
    return home_dir


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    fake = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: fake)
    return fake


def _seed(
    home: Path,
    tmp_path: Path,
    records: list[dict[str, object]],
    *,
    task_status: QueueItemStatus = QueueItemStatus.RUNNING,
    task_session_id: str | None = _SID,
) -> tuple[CwState, Path]:
    """Persist one live headless session, its dev-queue row, and a transcript."""
    worktree = tmp_path / "wt-2324"
    session = _mk_headless_daemon_session(
        _SID, worktree, _STARTED_AT, surface_ref=_SURFACE
    )
    state = CwState(sessions=[session])
    save_state(state)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=_SID,
                    client=_CLIENT,
                    status=task_status,
                    session_id=task_session_id,
                )
            ]
        )
    )
    transcript = _write_transcript_records(home, worktree, records)
    _stamp_after_start(transcript)
    return state, transcript


def _stamp_after_start(transcript: Path) -> None:
    after_ts = _STARTED_AT.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))


def _append_record(transcript: Path, record: dict[str, object]) -> None:
    with transcript.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    _stamp_after_start(transcript)


def _task_by_ticket() -> dict[str, Any]:
    return {t.ticket_id: t for t in load_dev_queue().tasks}


def _detect(state: CwState, native_live: set[str] | None = None) -> list[Any]:
    return _detect_mid_turn_usage_limit_candidates(
        state,
        native_live={_SURFACE} if native_live is None else native_live,
        task_by_ticket=_task_by_ticket(),
    )


def _act(
    state: CwState,
    candidates: list[Any],
    config: OrchestratorConfig,
) -> list[str]:
    with freezegun.freeze_time(_NOW):
        return _act_on_mid_turn_usage_limit_candidates(
            state,
            candidates,
            native_live={_SURFACE},
            clients={},
            config=config,
            now=_NOW,
        )


def _events(event_type: OrchestratorEventType) -> list[dict[str, Any]]:
    return [e.payload for e in read_events(event_types=[event_type])]


def _lockout() -> dict[str, datetime]:
    with freezegun.freeze_time(_NOW):
        return load_usage_limited_until()


# ---------------------------------------------------------------------------
# Detect phase
# ---------------------------------------------------------------------------


def test_detect_fires_on_limit_tail_of_live_session(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    candidates = _detect(state)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.session_id == _SID
    assert candidate.ticket_id == _SID
    assert candidate.client == _CLIENT
    assert candidate.proposed_action is ProposedAction.REVERT_TASK
    assert candidate.reap_reason is ReapReason.USAGE_LIMIT_MID_TURN
    assert candidate.usage_limit_detected is True


def test_detect_skips_when_any_sentinel_in_transcript(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    body = json.dumps(_shipped_salvage_payload())
    sentinel_text = f"narrative\n<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
    records = [
        _user_record("implement the plan", _T_USER),
        _ul_record(sentinel_text, _T_WORK),
        _limit_record(),
        *_trailing_metadata(),
    ]
    state, _ = _seed(home, tmp_path, records)

    assert _detect(state) == []


def test_detect_skips_when_limit_text_is_not_the_tail(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    records = [*_limit_tail(), _ul_record("recovered, continuing", _T_AFTER)]
    state, _ = _seed(home, tmp_path, records)

    assert _detect(state) == []


def test_detect_skips_tail_without_limit_text(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    records = [
        _user_record("implement the plan", _T_USER),
        _ul_record("all done here", _T_LIMIT),
    ]
    state, _ = _seed(home, tmp_path, records)

    assert _detect(state) == []


def test_detect_skips_phantom_session(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    assert _detect(state, native_live={"someone-else"}) == []


@pytest.mark.parametrize(
    ("task_status", "task_session_id"),
    [
        pytest.param(QueueItemStatus.BLOCKED_ON_USER, _SID, id="already-parked"),
        pytest.param(QueueItemStatus.PENDING, None, id="already-reverted"),
        pytest.param(QueueItemStatus.RUNNING, "other-session", id="foreign-owner"),
    ],
)
def test_detect_skips_row_not_running_or_not_owned(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    task_status: QueueItemStatus,
    task_session_id: str | None,
) -> None:
    state, _ = _seed(
        home,
        tmp_path,
        _limit_tail(),
        task_status=task_status,
        task_session_id=task_session_id,
    )

    assert _detect(state) == []


def test_detect_skips_session_without_ticket_row(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    save_dev_queue(DevQueueStore(tasks=[]))

    assert _detect(state) == []


def test_detect_fires_on_unparseable_reset_text(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail(_UNPARSEABLE_LIMIT_TEXT))

    assert len(_detect(state)) == 1


def test_detect_skips_when_latest_limit_match_has_no_timestamp(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """Fail closed: an untimestamped latest match makes the zero gap unknown.

    The older timestamped limit record is also the last timestamped content
    record, so borrowing its timestamp for the newer, untimestamped match
    would fake a zero gap. The gap is unknown instead, so nothing fires.
    """
    records = [
        _user_record("implement the plan", _T_USER),
        _limit_record(),
        _ul_record(_LIMIT_TEXT, None),
        *_trailing_metadata(),
    ]
    state, _ = _seed(home, tmp_path, records)

    assert _detect(state) == []


# ---------------------------------------------------------------------------
# Act phase -- reap_policy: auto
# ---------------------------------------------------------------------------


def test_act_auto_reverts_row_completes_session_and_arms_lockout(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto act runs in order: gate, arm, audit, stop, close, requeue."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    seen_at_stop: list[tuple[list[str], SessionStatus, QueueItemStatus]] = []
    real_stop = daemon.stop

    def _recording_stop(short_id: str) -> None:
        seen_at_stop.append(
            (
                [e.type.value for e in read_events()],
                load_state().sessions[0].status,
                load_dev_queue().tasks[0].status,
            )
        )
        real_stop(short_id)

    monkeypatch.setattr(daemon, "stop", _recording_stop)
    before = load_dev_queue().tasks[0].unproductive_attempts

    reverted = _act(state, _detect(state), _auto_config())

    assert reverted == [_SID]
    proposed = _events(OrchestratorEventType.SESSION_REAP_PROPOSED)
    assert len(proposed) == 1
    assert proposed[0]["proposed_action"] == "revert_task"
    assert proposed[0]["reason"] == "usage_limit_mid_turn"

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.unproductive_attempts == before
    assert task.session_id is None
    assert task.next_eligible_at == _RESET_AT

    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_at == _NOW
    assert session.completed_reason is CompletionReason.USAGE_LIMITED
    assert session.reap_reason is ReapReason.USAGE_LIMIT_MID_TURN

    completed = _events(OrchestratorEventType.SESSION_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["crashed"] is False
    assert completed[0]["ticket_id"] == _SID
    assert daemon.stop_calls == [_SURFACE]
    # Steps 2-3 (the lockout arm, then every audit event) all precede step 4's
    # stop; steps 5-6 (the session close and the requeue) both follow it.
    assert seen_at_stop == [
        (
            [
                OrchestratorEventType.USAGE_LIMIT_ARMED.value,
                OrchestratorEventType.SESSION_NEEDS_ATTENTION.value,
                OrchestratorEventType.SESSION_REAP_PROPOSED.value,
                OrchestratorEventType.SESSION_COMPLETED.value,
            ],
            SessionStatus.ACTIVE,
            QueueItemStatus.RUNNING,
        )
    ]

    armed = _events(OrchestratorEventType.USAGE_LIMIT_ARMED)
    assert armed == [
        {"client": _CLIENT, "until": _RESET_AT.isoformat(), "source": "parsed_reset"}
    ]
    assert _lockout() == {_CLIENT: _RESET_AT}

    attention = _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == 1
    assert attention[0]["paused_status"] == "usage_limited_mid_turn"
    assert _RESET_AT.isoformat() in attention[0]["breadcrumbs"]
    assert "re-enter the queue automatically" in attention[0]["breadcrumbs"]


def _assert_session_still_active(state: CwState) -> None:
    """Step 5 did not run: the session is ACTIVE in memory and on disk."""
    assert state.sessions[0].status is SessionStatus.ACTIVE
    persisted = load_state().sessions[0]
    assert persisted.status is SessionStatus.ACTIVE
    assert persisted.completed_at is None
    assert persisted.completed_reason is None
    assert persisted.reap_reason is None


def _assert_row_still_running() -> None:
    """Step 6 did not run: the row is still RUNNING and bound to the session."""
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == _SID
    assert task.next_eligible_at is None


def _log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_act_logs_and_continues_when_lockout_arm_fails(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 2 failure: log and continue, since the next tick re-arms."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)

    def _failing_record_event(
        etype: OrchestratorEventType,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> None:
        msg = "inbox write failed"
        raise OSError(msg)

    monkeypatch.setattr("cw.dispatch_state.record_event", _failing_record_event)

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    # The shared helper audits before it persists, so no window was saved.
    assert _lockout() == {}
    assert any(_SID in m and "lockout" in m for m in _log_messages(caplog))
    # Steps 3-6 still ran.
    assert reverted == [_SID]
    assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
    assert daemon.stop_calls == [_SURFACE]
    assert load_state().sessions[0].status is SessionStatus.COMPLETED
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


@pytest.mark.parametrize(
    "failing_event",
    [
        pytest.param(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION, id="needs-attention"
        ),
        pytest.param(OrchestratorEventType.SESSION_REAP_PROPOSED, id="reap-proposed"),
        pytest.param(OrchestratorEventType.SESSION_COMPLETED, id="completed"),
    ],
)
def test_act_auto_stops_at_step_3_when_an_audit_emit_fails(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_event: OrchestratorEventType,
) -> None:
    """Step 3 failure: no stop, no close, no requeue; the next tick retries."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)

    def _failing_record_event(
        etype: OrchestratorEventType,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> None:
        if etype is failing_event:
            msg = "inbox write failed"
            raise OSError(msg)
        record_event(etype, payload, correlation_id=correlation_id)

    # The reap proposal is recorded through _shared's binding, the rest here.
    for target in (
        "cw.reconcile.usage_limit_mid_turn.record_event",
        "cw.reconcile._shared.record_event",
    ):
        monkeypatch.setattr(target, _failing_record_event)

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert any(_SID in m and "audit" in m for m in _log_messages(caplog))
    # Step 2 ran before the failed emit.
    assert _lockout() == {_CLIENT: _RESET_AT}
    assert _events(failing_event) == []
    # Steps 4-6 did not.
    assert daemon.stop_calls == []
    _assert_session_still_active(state)
    _assert_row_still_running()
    cast("MagicMock", _deps.fire_push_notification).assert_not_called()


def test_act_auto_stops_at_step_4_when_daemon_stop_fails(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 4 failure: the session stays ACTIVE and the row stays RUNNING."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)

    def _failing_stop(short_id: str) -> None:
        daemon.stop_calls.append(short_id)
        msg = "claude stop failed"
        raise OSError(msg)

    monkeypatch.setattr(daemon, "stop", _failing_stop)

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert daemon.stop_calls == [_SURFACE]
    assert any(_SID in m and "stop" in m for m in _log_messages(caplog))
    # Steps 2-3 ran: the lockout is already armed for the retry.
    assert _lockout() == {_CLIENT: _RESET_AT}
    assert len(_events(OrchestratorEventType.SESSION_REAP_PROPOSED)) == 1
    assert len(_events(OrchestratorEventType.SESSION_COMPLETED)) == 1
    # Steps 5-6 did not.
    _assert_session_still_active(state)
    _assert_row_still_running()


def test_act_auto_leaves_session_closed_when_requeue_loses_race(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 6 race loss: log, emit nothing further, keep the session closed."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    real_stop = daemon.stop
    events_at_stop: list[int] = []

    def _stop_then_reclaim(short_id: str) -> None:
        real_stop(short_id)
        events_at_stop.append(len(read_events()))
        store = load_dev_queue()
        store.tasks[0].session_id = "reclaimer"
        save_dev_queue(store)

    monkeypatch.setattr(daemon, "stop", _stop_then_reclaim)

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert any(_SID in m and "race" in m for m in _log_messages(caplog))
    # Step 5 ran: the session is closed, which is right -- its process is gone.
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason is CompletionReason.USAGE_LIMITED
    # Step 6 lost: the reclaimer's row is untouched.
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "reclaimer"
    assert task.next_eligible_at is None
    # Nothing was emitted after the stop.
    assert len(read_events()) == events_at_stop[0]


def test_act_auto_falls_back_to_flat_backoff_on_unparseable_reset(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail(_UNPARSEABLE_LIMIT_TEXT))
    config = _auto_config(usage_limit_backoff_seconds=_BACKOFF_SECONDS)

    reverted = _act(state, _detect(state), config)

    expected_until = _NOW + timedelta(seconds=_BACKOFF_SECONDS)
    assert reverted == [_SID]
    assert load_dev_queue().tasks[0].next_eligible_at == expected_until
    armed = _events(OrchestratorEventType.USAGE_LIMIT_ARMED)
    assert armed == [
        {
            "client": _CLIENT,
            "until": expected_until.isoformat(),
            "source": "flat_backoff",
        }
    ]
    assert _lockout() == {_CLIENT: expected_until}


def test_act_auto_is_noop_when_tail_changed_since_detect(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, transcript = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    assert len(candidates) == 1
    _append_record(transcript, _ul_record("back again, continuing", _T_AFTER))

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert _events(OrchestratorEventType.SESSION_REAP_PROPOSED) == []
    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
    assert load_state().sessions[0].status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []
    assert _lockout() == {}
    messages = [r.getMessage() for r in caplog.records]
    assert any(_SID in m and "tail changed" in m for m in messages)


# ---------------------------------------------------------------------------
# Act phase -- reap_policy: signal_only (default)
# ---------------------------------------------------------------------------


def test_act_signal_only_parks_blocked_on_user_without_touching_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    before = load_dev_queue().tasks[0].unproductive_attempts

    reverted = _act(state, _detect(state), OrchestratorConfig())

    assert reverted == []
    proposed = _events(OrchestratorEventType.SESSION_REAP_PROPOSED)
    assert len(proposed) == 1
    assert proposed[0]["reason"] == "usage_limit_mid_turn"

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "usage_limited_mid_turn"
    assert task.unproductive_attempts == before
    assert task.session_id == _SID
    assert task.next_eligible_at is None

    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.surface_ref == _SURFACE
    assert session.completed_reason is None
    assert daemon.stop_calls == []
    assert _events(OrchestratorEventType.SESSION_COMPLETED) == []

    assert _lockout() == {_CLIENT: _RESET_AT}
    attention = _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert len(attention) == 1
    assert attention[0]["paused_status"] == "usage_limited_mid_turn"
    assert attention[0]["ticket_id"] == _SID
    assert attention[0]["crashed"] is False
    assert _RESET_AT.isoformat() in attention[0]["breadcrumbs"]
    assert "needs an operator" in attention[0]["breadcrumbs"]
    push = cast("MagicMock", _deps.fire_push_notification)
    push.assert_called_once_with(load_state().sessions[0].name, _CLIENT)


# ---------------------------------------------------------------------------
# Act phase -- race re-verify under dev_queue_lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(_auto_config(), id="auto"),
        pytest.param(OrchestratorConfig(), id="signal_only"),
    ],
)
@pytest.mark.parametrize(
    ("raced_status", "raced_session_id"),
    [
        pytest.param(QueueItemStatus.COMPLETED, _SID, id="row-completed"),
        pytest.param(QueueItemStatus.RUNNING, "reclaimer", id="row-reclaimed"),
    ],
)
def test_act_is_silent_noop_when_row_moved_between_detect_and_act(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    config: OrchestratorConfig,
    raced_status: QueueItemStatus,
    raced_session_id: str,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    store = load_dev_queue()
    store.tasks[0].status = raced_status
    store.tasks[0].session_id = raced_session_id
    save_dev_queue(store)

    reverted = _act(state, candidates, config)

    assert reverted == []
    task = load_dev_queue().tasks[0]
    assert task.status is raced_status
    assert task.session_id == raced_session_id
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.reap_proposed_at is None
    assert state.sessions[0].reap_proposed_at is None
    assert daemon.stop_calls == []
    assert _events(OrchestratorEventType.SESSION_REAP_PROPOSED) == []
    assert _events(OrchestratorEventType.SESSION_COMPLETED) == []
    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []
    assert _lockout() == {}


def test_act_signal_only_proposes_nothing_when_park_loses_race_after_gate(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is reclaimed between the gate and the park: steps 1-2 already
    ran (the lockout stays armed), but the park and its proposal do not."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)

    def _arm_then_reclaim(windows: dict[str, datetime]) -> dict[str, datetime]:
        merged = merge_and_save_usage_limited_until(windows)
        store = load_dev_queue()
        store.tasks[0].session_id = "reclaimer"
        save_dev_queue(store)
        return merged

    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.merge_and_save_usage_limited_until",
        _arm_then_reclaim,
    )

    reverted = _act(state, candidates, OrchestratorConfig())

    assert reverted == []
    assert _lockout() == {_CLIENT: _RESET_AT}
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "reclaimer"
    assert state.sessions[0].reap_proposed_at is None
    assert _events(OrchestratorEventType.SESSION_REAP_PROPOSED) == []
    assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []


def test_act_skips_candidate_whose_session_vanished(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    state.sessions.clear()

    assert _act(state, candidates, _auto_config()) == []
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING
    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []


def test_act_auto_skips_stop_when_surface_ref_already_cleared(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    # Located by csid instead, so the transcript is still found.
    state.sessions[0].claude_session_id = "fake-short-id-sess-1076"
    state.sessions[0].surface_ref = None

    assert _act(state, candidates, _auto_config()) == [_SID]
    assert daemon.stop_calls == []
    assert load_state().sessions[0].status is SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def test_detect_and_park_loads_queue_when_task_by_ticket_omitted(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    with freezegun.freeze_time(_NOW):
        reverted = detect_and_park_mid_turn_usage_limits(
            state,
            now=_NOW,
            native_live={_SURFACE},
            config=_auto_config(),
            clients={},
        )

    assert reverted == [_SID]
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


def test_detect_and_park_is_idempotent_on_second_tick(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    for _ in range(2):
        with freezegun.freeze_time(_NOW):
            detect_and_park_mid_turn_usage_limits(
                state,
                now=_NOW,
                native_live={_SURFACE},
                config=OrchestratorConfig(),
                clients={},
            )

    assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
    assert len(_events(OrchestratorEventType.USAGE_LIMIT_ARMED)) == 1
