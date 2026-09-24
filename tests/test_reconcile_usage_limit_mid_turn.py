"""Unit tests for cw.reconcile.usage_limit_mid_turn (#2324).

A still-roster-present worker whose transcript tail is a usage-limit message
with no sentinel is detected, the client's ``usage_limited_until`` lockout is
armed, and the dev-queue row is dispositioned without an unproductive-attempt
charge -- RUNNING->PENDING (+ ``next_eligible_at``) under ``reap_policy: auto``,
RUNNING->BLOCKED_ON_USER under ``signal_only``. The act is decided by one
write-ahead intent on the row and resumed from it on any later tick.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import freezegun
import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue, transition_task_status
from cw.dispatch_state import (
    load_usage_limited_until,
    merge_and_save_usage_limited_until,
)
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    SessionStatus,
    TicketTask,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    ProposedAction,
    _deps,
    detect_and_park_mid_turn_usage_limits,
)
from cw.reconcile import usage_limit_mid_turn as mid_turn
from cw.reconcile.usage_limit_mid_turn import (
    ACT_STARTED_AT_KEY,
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

if TYPE_CHECKING:
    from collections.abc import Callable

# Verbatim from the #2324 ticket: the text all three stalled workers ended on.
_LIMIT_TEXT = "You've hit your weekly limit · resets Sep 26, 11pm (America/New_York)"
_UNPARSEABLE_LIMIT_TEXT = "You've hit your weekly limit · resets soon"

_NY = ZoneInfo("America/New_York")
_SID = "2324-mid"
_SURFACE = "fake-short-id"
_CLIENT = "client-a"
_STARTED_AT = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(minutes=10)
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
    """A fake daemon whose roster lists the worker until it is stopped."""
    fake = FakeNativeDaemonClient()
    fake._live.add(_SURFACE)
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


def _detect(state: CwState, native_live: set[str] | None = None) -> list[Any]:
    return _detect_mid_turn_usage_limit_candidates(
        state,
        native_live={_SURFACE} if native_live is None else native_live,
        tasks=load_dev_queue().tasks,
    )


def _owned_row() -> TicketTask:
    """This session's own row, found by its (ticket, client) key."""
    return next(
        t for t in load_dev_queue().tasks if t.ticket_id == _SID and t.client == _CLIENT
    )


def _save_tasks_around_owned_row(
    *, before: list[TicketTask], after: list[TicketTask]
) -> None:
    """Re-save the queue with *before* and *after* rows around the owned row."""
    owned = load_dev_queue().tasks[0]
    save_dev_queue(DevQueueStore(tasks=[*before, owned, *after]))


def _running_row(*, client: str, session_id: str) -> TicketTask:
    return _make_ticket_task(
        ticket_id=_SID,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
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


def _tick(
    state: CwState,
    config: OrchestratorConfig,
    daemon: FakeNativeDaemonClient,
    *,
    at: datetime,
) -> list[str]:
    """One reconcile pass of the sweep, with the roster as the fake reports it."""
    with freezegun.freeze_time(at):
        return detect_and_park_mid_turn_usage_limits(
            state,
            now=at,
            native_live=daemon.list_live_session_short_ids(),
            config=config,
            clients={},
        )


def _events(event_type: OrchestratorEventType) -> list[dict[str, Any]]:
    return [e.payload for e in read_events(event_types=[event_type])]


def _lockout() -> dict[str, datetime]:
    with freezegun.freeze_time(_NOW):
        return load_usage_limited_until()


def _log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def _assert_session_still_active(state: CwState) -> None:
    """The session was not closed: it is ACTIVE in memory and on disk."""
    assert state.sessions[0].status is SessionStatus.ACTIVE
    persisted = load_state().sessions[0]
    assert persisted.status is SessionStatus.ACTIVE
    assert persisted.completed_at is None
    assert persisted.completed_reason is None
    assert persisted.reap_reason is None


def _assert_row_still_running() -> None:
    """The row was not transitioned: RUNNING and bound to the session."""
    task = _owned_row()
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == _SID
    assert task.next_eligible_at is None


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


def test_detect_skips_when_later_content_timestamp_is_unknown(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """An untimestamped later record makes the apparent zero gap unknown."""
    records = [*_limit_tail(), _ul_record("worker continued", None)]
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


def test_detect_skips_row_already_carrying_an_act(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A row with an act in flight is resumed from its intent, never re-decided."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    store = load_dev_queue()
    store.tasks[0].usage_limit_act = UsageLimitAct(
        session_id=_SID,
        branch="park",
        started_at=_NOW,
        reset_at=_RESET_AT,
        until=_RESET_AT,
    )
    save_dev_queue(store)

    assert _detect(state) == []


@pytest.mark.parametrize(
    "shadow",
    [
        pytest.param(
            _running_row(client="client-b", session_id="b-session"),
            id="other-client-same-ticket",
        ),
        pytest.param(
            _running_row(client=_CLIENT, session_id="duplicate-session"),
            id="duplicate-running-row",
        ),
    ],
)
def test_detect_finds_owned_row_despite_a_later_same_ticket_row(
    tmp_config_dir: Path, tmp_path: Path, home: Path, shadow: TicketTask
) -> None:
    """The row is keyed on (ticket_id, client, session_id), not ticket_id (#2219).

    A same-ticket RUNNING row after the owned one -- another client's ticket
    with the same id, or a duplicate RUNNING row -- must not shadow it.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    _save_tasks_around_owned_row(before=[], after=[shadow])

    candidates = _detect(state)

    assert [c.session_id for c in candidates] == [_SID]


def test_detect_skips_row_owned_by_session_under_another_client(
    tmp_config_dir: Path, tmp_path: Path, home: Path
) -> None:
    """A row matching ticket and session but not the session's client is not its."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    save_dev_queue(
        DevQueueStore(tasks=[_running_row(client="client-b", session_id=_SID)])
    )

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
# Act -- reap_policy: auto
# ---------------------------------------------------------------------------


def test_act_auto_reverts_row_completes_session_and_arms_lockout(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decide, then arm, audit, stop, close and requeue, in that order."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    seen_at_stop: list[
        tuple[list[str], SessionStatus, QueueItemStatus, UsageLimitAct | None]
    ] = []
    real_stop = daemon.stop

    def _recording_stop(short_id: str) -> None:
        row = load_dev_queue().tasks[0]
        seen_at_stop.append(
            (
                [e.type.value for e in read_events()],
                load_state().sessions[0].status,
                row.status,
                row.usage_limit_act,
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
    # The requeue cleared the intent in the same write.
    assert task.usage_limit_act is None

    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_at == _NOW
    assert session.completed_reason is CompletionReason.USAGE_LIMITED
    assert session.reap_reason is ReapReason.USAGE_LIMIT_MID_TURN

    completed = _events(OrchestratorEventType.SESSION_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["crashed"] is False
    assert completed[0]["ticket_id"] == _SID
    # Marks the event as reconcile-owned so the dispatch consumer skips it.
    assert completed[0]["reason"] == "usage_limited_mid_turn"
    assert completed[0][ACT_STARTED_AT_KEY] == _NOW.isoformat()
    assert daemon.stop_calls == [_SURFACE]
    # The intent was persisted first, and by the stop the lockout arm and
    # every audit event had landed; the session close and the requeue follow.
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
            UsageLimitAct(
                session_id=_SID,
                branch="auto",
                started_at=_NOW,
                reset_at=_RESET_AT,
                until=_RESET_AT,
                audited_at=_NOW,
                stop_started_at=_NOW,
            ),
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
    assert attention[0][ACT_STARTED_AT_KEY] == _NOW.isoformat()
    assert _RESET_AT.isoformat() in attention[0]["breadcrumbs"]
    assert "re-enter the queue automatically" in attention[0]["breadcrumbs"]


def test_stop_holds_queue_ownership_through_stop_invocation(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The external stop runs after the queue lock releases."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    intent = mid_turn._decide(
        state.sessions[0],
        _SID,
        branch="auto",
        config=_auto_config(),
        now=_NOW,
    )
    assert intent is not None
    row = mid_turn._ActRow(
        ticket_id=_SID, client=_CLIENT, lane="default", intent=intent
    )
    act = mid_turn._Act(
        row=row,
        state=state,
        session=state.sessions[0],
        native_live={_SURFACE},
        now=_NOW,
    )

    class _ClearIntentOnUnlock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            store = load_dev_queue()
            store.tasks[0].usage_limit_act = None
            save_dev_queue(store)

    monkeypatch.setattr(mid_turn, "dev_queue_lock", _ClearIntentOnUnlock)
    real_stop = daemon.stop

    def _stop(short_id: str) -> None:
        # The lock scope has ended before the external daemon call. A
        # concurrent disposition may clear the intent; the post-stop fence
        # check then prevents this act from closing or requeuing the row.
        assert load_dev_queue().tasks[0].usage_limit_act is None
        real_stop(short_id)

    monkeypatch.setattr(daemon, "stop", _stop)

    assert mid_turn._stop_surface(act) is mid_turn._Stop.DONE
    assert daemon.stop_calls == [_SURFACE]


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


def test_act_decides_nothing_when_tail_changed_since_detect(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate re-reads the tail: a changed one writes no intent at all."""
    state, transcript = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    assert len(candidates) == 1
    _append_record(transcript, _ul_record("back again, continuing", _T_AFTER))

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert read_events() == []
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is None
    assert load_state().sessions[0].status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []
    assert _lockout() == {}
    assert any(
        _SID in m and "tail changed since detect" in m for m in _log_messages(caplog)
    )


def test_act_auto_abandons_when_tail_changes_before_the_stop(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker resumes after the decision: the act is abandoned cleanly.

    The tail is re-read right before the stop. New content clears the intent
    and leaves the session and row exactly as they are -- nothing stopped,
    closed or requeued -- and a later tick finds nothing to resume.
    """
    state, transcript = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)

    def _arm_then_resume(windows: dict[str, datetime]) -> dict[str, datetime]:
        merged = merge_and_save_usage_limited_until(windows)
        _append_record(transcript, _ul_record("back again, continuing", _T_AFTER))
        return merged

    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.merge_and_save_usage_limited_until",
        _arm_then_resume,
    )

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert daemon.stop_calls == []
    assert any(_SID in m and "abandoned" in m for m in _log_messages(caplog))
    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is None
    events_after = len(read_events())

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    assert daemon.stop_calls == []
    _assert_row_still_running()
    assert len(read_events()) == events_after


def test_act_auto_retries_stop_while_surface_still_in_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop() that returned is not trusted: the roster must confirm it.

    The real client swallows every ``claude stop`` failure, so a surface still
    in the roster after the bounded poll leaves the session open and the row
    RUNNING under its intent; the next tick stops it again and finishes.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn._STOP_CONFIRM_TIMEOUT_SECS", 0.0
    )
    real_stop = daemon.stop
    # A stop that returns but never takes effect.
    monkeypatch.setattr(daemon, "stop", daemon.stop_calls.append)

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _tick(state, _auto_config(), daemon, at=_NOW) == []

    assert daemon.stop_calls == [_SURFACE]
    assert any(_SID in m and "roster" in m for m in _log_messages(caplog))
    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    monkeypatch.setattr(daemon, "stop", real_stop)

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]
    assert daemon.stop_calls == [_SURFACE, _SURFACE]
    assert _owned_row().status is QueueItemStatus.PENDING
    assert load_state().sessions[0].completed_at == _LATER


def test_act_auto_retries_stop_when_roster_unreadable(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable roster cannot confirm the stop, so it fails closed."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn._STOP_CONFIRM_TIMEOUT_SECS", 0.0
    )
    daemon.roster_unreadable = True

    assert _act(state, candidates, _auto_config()) == []
    assert daemon.stop_calls == [_SURFACE]
    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None


def test_act_auto_requeues_only_the_row_keyed_to_the_sessions_client(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """An earlier same-ticket row under another client is never the one mutated.

    It matches the ticket and even the session id, so a ticket-and-session
    lookup would take it first; only (ticket_id, client, session_id) is the
    owned row's identity (#2219).
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    foreign = _running_row(client="client-b", session_id=_SID)
    _save_tasks_around_owned_row(before=[foreign], after=[])

    assert _act(state, _detect(state), _auto_config()) == [_SID]

    tasks = load_dev_queue().tasks
    assert tasks[0].client == "client-b"
    assert tasks[0].status is QueueItemStatus.RUNNING
    assert tasks[0].session_id == _SID
    assert tasks[0].usage_limit_act is None
    assert tasks[1].client == _CLIENT
    assert tasks[1].status is QueueItemStatus.PENDING
    assert tasks[1].next_eligible_at == _RESET_AT


def test_act_decides_on_the_session_matched_row_of_a_duplicate_pair(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """``_decide`` writes the intent onto the session's own duplicate row.

    An earlier duplicate RUNNING row for the same ``(ticket_id, client)`` is
    reachable via add-after-terminal plus ``requeue --from-completed``
    (#2219); a first-match lookup would bind the act to it. Only the row
    stamped with this session's id is the act's.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    duplicate = _running_row(client=_CLIENT, session_id="duplicate-session")
    _save_tasks_around_owned_row(before=[duplicate], after=[])

    assert _act(state, _detect(state), _auto_config()) == [_SID]

    tasks = load_dev_queue().tasks
    assert tasks[0].session_id == "duplicate-session"
    assert tasks[0].status is QueueItemStatus.RUNNING
    assert tasks[0].usage_limit_act is None
    assert tasks[0].next_eligible_at is None
    assert tasks[1].status is QueueItemStatus.PENDING
    assert tasks[1].next_eligible_at == _RESET_AT


def test_act_ends_when_another_writer_dispositions_the_row_mid_act(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator cancel mid-act clears the intent, so the act ends there.

    Neither the session close nor the requeue lands: once the row no longer
    carries the intent, the act owns neither, so the session is left open
    and the cancelled row exactly as the operator left it.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    real_stop = daemon.stop

    def _stop_then_cancel(short_id: str) -> None:
        real_stop(short_id)
        store = load_dev_queue()
        transition_task_status(store.tasks[0], QueueItemStatus.CANCELLED)
        save_dev_queue(store)

    monkeypatch.setattr(daemon, "stop", _stop_then_cancel)

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert any(_SID in m and "no longer carries" in m for m in _log_messages(caplog))
    _assert_session_still_active(state)
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.CANCELLED
    assert task.usage_limit_act is None
    assert task.next_eligible_at is None


def test_act_leaves_session_open_when_row_is_requeued_after_the_stop_confirms(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closing the session re-checks ownership under the lock first.

    The stop confirmation polls the roster for up to several seconds, and an
    operator requeue can land inside that window. Once the row no longer
    carries this act's intent, the act owns neither it nor the session: the
    session is not stamped COMPLETED, ``save_state`` is not called, and the
    row is left exactly as the operator left it.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    candidates = _detect(state)
    real_wait = mid_turn.wait_for_roster_presence

    def _confirm_then_requeue(*args: Any, **kwargs: Any) -> bool:
        confirmed = real_wait(*args, **kwargs)
        store = load_dev_queue()
        transition_task_status(store.tasks[0], QueueItemStatus.PENDING)
        store.tasks[0].session_id = None
        save_dev_queue(store)
        return confirmed

    saves: list[CwState] = []
    monkeypatch.setattr(mid_turn, "wait_for_roster_presence", _confirm_then_requeue)
    monkeypatch.setattr(mid_turn, "save_state", saves.append)

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config())

    assert reverted == []
    assert daemon.stop_calls == [_SURFACE]
    assert saves == []
    _assert_session_still_active(state)
    assert any(_SID in m and "no longer carries" in m for m in _log_messages(caplog))
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.session_id is None
    assert task.usage_limit_act is None
    assert task.next_eligible_at is None


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
# Act -- reap_policy: signal_only (default)
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
    assert task.usage_limit_act is None

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
    assert attention[0][ACT_STARTED_AT_KEY] == _NOW.isoformat()
    assert _RESET_AT.isoformat() in attention[0]["breadcrumbs"]
    assert "needs an operator" in attention[0]["breadcrumbs"]
    push = cast("MagicMock", _deps.fire_push_notification)
    push.assert_called_once_with(load_state().sessions[0].name, _CLIENT)


# ---------------------------------------------------------------------------
# Gate -- re-verified under dev_queue_lock before anything is decided
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
    assert task.usage_limit_act is None
    session = load_state().sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.reap_proposed_at is None
    assert state.sessions[0].reap_proposed_at is None
    assert daemon.stop_calls == []
    assert read_events() == []
    assert _lockout() == {}


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
    assert _owned_row().status is QueueItemStatus.RUNNING
    assert _owned_row().usage_limit_act is None
    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def test_detect_and_park_loads_queue_when_tasks_omitted(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    assert _tick(state, _auto_config(), daemon, at=_NOW) == [_SID]
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


def test_detect_and_park_is_idempotent_on_second_tick(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())

    for _ in range(2):
        _tick(state, OrchestratorConfig(), daemon, at=_NOW)

    assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
    assert len(_events(OrchestratorEventType.USAGE_LIMIT_ARMED)) == 1
    assert len(_events(OrchestratorEventType.TASK_TRANSITION)) == 1


# ---------------------------------------------------------------------------
# Resumability -- an interruption after any step is finished by the next tick
# ---------------------------------------------------------------------------


def _fail_once(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    real: Callable[..., Any],
    *,
    nth: int = 1,
    match: Callable[..., bool] = lambda *_a, **_k: True,
) -> None:
    """Patch *target* so its *nth* matching call raises OSError; others delegate."""
    seen: list[int] = []

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        if match(*args, **kwargs):
            seen.append(1)
            if len(seen) == nth:
                msg = f"injected failure in {target}"
                raise OSError(msg)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, _wrapper)


def _is_event(etype: OrchestratorEventType) -> Callable[..., bool]:
    return lambda event_type, *_a, **_k: event_type is etype


def _inject(
    point: str,
    monkeypatch: pytest.MonkeyPatch,
    daemon: FakeNativeDaemonClient,
    *,
    branch: str,
) -> None:
    """Make the act fail once at *point*, the way a crash there would leave it.

    The module's dev-queue writes are, in order: the decision (1), the audit
    mark (2) and the final transition (3).
    """
    from cw import dispatch_state
    from cw.config import save_state as real_save_state
    from cw.events import record_event as real_record_event

    mod = "cw.reconcile.usage_limit_mid_turn"
    if point == "decision":
        _fail_once(monkeypatch, f"{mod}.save_dev_queue", save_dev_queue, nth=1)
    elif point == "lockout":
        _fail_once(
            monkeypatch, "cw.dispatch_state.record_event", dispatch_state.record_event
        )
    elif point == "needs-attention":
        _fail_once(
            monkeypatch,
            f"{mod}.record_event",
            real_record_event,
            match=_is_event(OrchestratorEventType.SESSION_NEEDS_ATTENTION),
        )
    elif point == "reap-proposed":
        _fail_once(
            monkeypatch,
            "cw.reconcile._shared.record_event",
            real_record_event,
            match=_is_event(OrchestratorEventType.SESSION_REAP_PROPOSED),
        )
    elif point == "completed":
        _fail_once(
            monkeypatch,
            f"{mod}.record_event",
            real_record_event,
            match=_is_event(OrchestratorEventType.SESSION_COMPLETED),
        )
    elif point == "audit-mark":
        _fail_once(monkeypatch, f"{mod}.save_dev_queue", save_dev_queue, nth=2)
    elif point == "stop":
        real_stop = daemon.stop
        calls: list[int] = []

        def _stop_failing_once(short_id: str) -> None:
            calls.append(1)
            if len(calls) == 1:
                msg = "claude stop failed"
                raise OSError(msg)
            real_stop(short_id)

        monkeypatch.setattr(daemon, "stop", _stop_failing_once)
    elif point == "close":
        _fail_once(monkeypatch, f"{mod}.save_state", real_save_state)
    elif point == "transition":
        _fail_once(
            monkeypatch,
            f"{mod}.save_dev_queue",
            save_dev_queue,
            nth=4 if branch == "auto" else 3,
        )
    else:  # pragma: no cover - a typo in the parametrize list
        msg = f"unknown injection point {point!r}"
        raise AssertionError(msg)


_COMMON_POINTS = [
    "decision",
    "lockout",
    "needs-attention",
    "reap-proposed",
    "audit-mark",
    "transition",
]
_AUTO_ONLY_POINTS = ["completed", "stop", "close"]


@pytest.mark.parametrize(
    ("branch", "point"),
    [
        *[pytest.param("auto", p, id=f"auto-{p}") for p in _COMMON_POINTS],
        *[pytest.param("auto", p, id=f"auto-{p}") for p in _AUTO_ONLY_POINTS],
        *[pytest.param("park", p, id=f"park-{p}") for p in _COMMON_POINTS],
    ],
)
def test_interrupted_act_is_finished_next_tick_without_charge(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    point: str,
) -> None:
    """A step fails once; the next tick resumes from the intent and finishes.

    No attempt is charged, and each side effect -- the lockout arm, the stop,
    the session close and the row transition -- happens at most once. Only
    the audit events may repeat, and every copy carries the same act key.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    config = _auto_config() if branch == "auto" else OrchestratorConfig()
    before = _owned_row().unproductive_attempts
    _inject(point, monkeypatch, daemon, branch=branch)

    assert _tick(state, config, daemon, at=_NOW) == []
    _assert_row_still_running()

    requeued = _tick(load_state(), config, daemon, at=_LATER)

    decided_at = _LATER if point == "decision" else _NOW
    task = _owned_row()
    assert task.unproductive_attempts == before
    assert task.usage_limit_act is None
    session = load_state().sessions[0]
    if branch == "auto":
        assert requeued == [_SID]
        assert task.status is QueueItemStatus.PENDING
        assert task.session_id is None
        assert task.next_eligible_at == _RESET_AT
        assert session.status is SessionStatus.COMPLETED
        assert session.completed_reason is CompletionReason.USAGE_LIMITED
        # Closed by whichever tick got past the stop: only a failed final
        # transition leaves the first tick's close in place.
        closed_at = _NOW if point == "transition" else _LATER
        assert session.completed_at == closed_at
        assert daemon.list_live_session_short_ids() == set()
    else:
        assert requeued == []
        assert task.status is QueueItemStatus.BLOCKED_ON_USER
        assert task.disposition == "usage_limited_mid_turn"
        assert task.session_id == _SID
        assert session.status is SessionStatus.ACTIVE
        assert daemon.stop_calls == []

    # At most once: the lockout, the stop that landed and the transition.
    assert len(_events(OrchestratorEventType.USAGE_LIMIT_ARMED)) == 1
    assert _lockout() == {_CLIENT: _RESET_AT}
    assert daemon.stop_calls.count(_SURFACE) == (1 if branch == "auto" else 0)
    # The transition seam records task.transition inside the lock, ahead of
    # the save, so a failed final save leaves one extra audit record for a
    # transition that did not persist. Neither copy charges an attempt.
    transitions = _events(OrchestratorEventType.TASK_TRANSITION)
    assert len(transitions) == (2 if point == "transition" else 1)
    assert {t["unproductive_charge"] for t in transitions} == {False}

    # At least once: every audit event landed, each keyed to the one act.
    audited = [OrchestratorEventType.SESSION_NEEDS_ATTENTION]
    if branch == "auto":
        audited.append(OrchestratorEventType.SESSION_COMPLETED)
    for etype in audited:
        payloads = _events(etype)
        assert 1 <= len(payloads) <= 2, etype
        assert {p[ACT_STARTED_AT_KEY] for p in payloads} == {decided_at.isoformat()}
    assert 1 <= len(_events(OrchestratorEventType.SESSION_REAP_PROPOSED)) <= 2

    # A third tick finds nothing left to do.
    events_after = len(read_events())
    assert _tick(load_state(), config, daemon, at=_LATER) == []
    assert _owned_row() == task
    assert len(read_events()) == events_after


def test_act_resumes_when_its_session_is_gone(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A row carrying an act whose session has left the state still finishes."""
    _seed(home, tmp_path, _limit_tail())
    store = load_dev_queue()
    store.tasks[0].usage_limit_act = UsageLimitAct(
        session_id=_SID,
        branch="auto",
        started_at=_NOW,
        reset_at=_RESET_AT,
        until=_RESET_AT,
        audited_at=_NOW,
    )
    save_dev_queue(store)
    save_state(CwState(sessions=[]))

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert any(_SID in m and "gone" in m for m in _log_messages(caplog))
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.usage_limit_act is None
    assert task.next_eligible_at == _RESET_AT


def test_act_retries_when_lockout_write_silently_does_not_land(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sidecar write swallows its own errors, so the window is read back.

    A window that did not persist stops the act before any audit or effect;
    the next tick arms it and finishes.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.merge_and_save_usage_limited_until",
        dict,
    )

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _tick(state, _auto_config(), daemon, at=_NOW) == []

    assert any(_SID in m and "did not persist" in m for m in _log_messages(caplog))
    assert _lockout() == {}
    assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []
    assert daemon.stop_calls == []
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.merge_and_save_usage_limited_until",
        merge_and_save_usage_limited_until,
    )

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]
    assert _lockout() == {_CLIENT: _RESET_AT}


def test_act_resumed_after_its_window_lapsed_skips_the_lockout(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """A window already over needs no lockout; the rest of the act still runs."""
    _seed(home, tmp_path, _limit_tail())
    store = load_dev_queue()
    store.tasks[0].usage_limit_act = UsageLimitAct(
        session_id=_SID,
        branch="auto",
        started_at=_NOW,
        reset_at=None,
        until=_LATER,
        audited_at=_NOW,
    )
    save_dev_queue(store)
    after_window = _LATER + timedelta(minutes=1)

    assert _tick(load_state(), _auto_config(), daemon, at=after_window) == [_SID]

    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.next_eligible_at == _LATER
    assert load_state().sessions[0].status is SessionStatus.COMPLETED
