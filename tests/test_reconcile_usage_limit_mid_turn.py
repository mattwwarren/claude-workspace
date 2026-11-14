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
from cw.reconcile._shared._roster import ROSTER_REGISTRATION_SLACK_SECONDS
from cw.reconcile.deferred import DeferredReconcileJobs, StopKind, run_post_lock_jobs
from cw.reconcile.leaked_workers import sweep_leaked_daemon_workers
from cw.reconcile.usage_limit_mid_turn import (
    ACT_STALLED_AFTER_SECONDS,
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
    events_doc_bullet,
)
from tests._shared.daemon_fakes import (
    _stop_leaves_worker_listed,
    _stop_makes_roster_unreadable,
)
from tests._shared.session_seeding import _make_ticket_task

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
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("cw.reconcile._deps.host_timezone", lambda: _NY)
    return Path.home()


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
    *,
    deferred: DeferredReconcileJobs | None = None,
) -> list[str]:
    jobs = DeferredReconcileJobs() if deferred is None else deferred
    with freezegun.freeze_time(_NOW):
        return _act_on_mid_turn_usage_limit_candidates(
            state,
            candidates,
            native_live={_SURFACE},
            clients={},
            config=config,
            now=_NOW,
            deferred=jobs,
        )


def _tick(
    state: CwState,
    config: OrchestratorConfig,
    daemon: FakeNativeDaemonClient,
    *,
    at: datetime,
    deferred: DeferredReconcileJobs | None = None,
    drain: bool = True,
    leaked_sweep: bool = False,
) -> list[str]:
    """One reconcile pass of the sweep, with the roster as the fake reports it.

    Mirrors the lock-free drain that follows the locked sweep: the queued
    post-lock stop runs after the pass unless *drain* is False. *leaked_sweep*
    runs the pre-guard leaked-worker sweep first, as ``reconcile()`` does.
    """
    jobs = DeferredReconcileJobs() if deferred is None else deferred
    with freezegun.freeze_time(at):
        if leaked_sweep:
            sweep_leaked_daemon_workers(
                state, daemon=daemon, now=at, clients={}, deferred=jobs
            )
        requeued = detect_and_park_mid_turn_usage_limits(
            state,
            now=at,
            native_live=daemon.list_live_session_short_ids(),
            config=config,
            clients={},
            deferred=jobs,
        )
    if drain:
        run_post_lock_jobs(jobs)
    return requeued


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
    body = json.dumps(_shipped_salvage_payload(_SID))
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

    jobs = DeferredReconcileJobs()
    assert (
        _tick(state, _auto_config(), daemon, at=_NOW, deferred=jobs, drain=False) == []
    )

    # Tick 1 closes the session and queues the stop; the row stays RUNNING
    # under its intent until a later tick confirms the surface is gone.
    assert daemon.stop_calls == []
    proposed = _events(OrchestratorEventType.SESSION_REAP_PROPOSED)
    assert len(proposed) == 1
    assert proposed[0]["proposed_action"] == "revert_task"
    assert proposed[0]["reason"] == "usage_limit_mid_turn"
    assert _owned_row().status is QueueItemStatus.RUNNING
    assert _owned_row().usage_limit_act is not None
    assert len(jobs.post_lock) == 1

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

    run_post_lock_jobs(jobs)
    assert daemon.stop_calls == [_SURFACE]

    reverted = _tick(load_state(), _auto_config(), daemon, at=_LATER)
    assert reverted == [_SID]

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.unproductive_attempts == before
    assert task.session_id is None
    assert task.next_eligible_at == _RESET_AT
    # The requeue cleared the intent in the same write.
    assert task.usage_limit_act is None
    # No second stop: the session was terminal by the confirming tick.
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
            SessionStatus.COMPLETED,
            QueueItemStatus.RUNNING,
            UsageLimitAct(
                session_id=_SID,
                branch="auto",
                started_at=_NOW,
                reset_at=_RESET_AT,
                until=_RESET_AT,
                audited_at=_NOW,
            ),
        )
    ]

    _assert_lockout_armed_and_attention()


def _assert_lockout_armed_and_attention() -> None:
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


def test_act_never_calls_stop_in_the_sweep(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep queues the stop; it never calls ``daemon.stop()`` itself (#2549).

    A stop that would raise if called proves the sweep is stop-free: the pass
    completes, the session is closed, and exactly one surface stop is queued
    for the post-lock drain.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())

    def _forbidden_stop(short_id: str) -> None:
        msg = f"stop called in the sweep: {short_id}"
        raise AssertionError(msg)

    monkeypatch.setattr(daemon, "stop", _forbidden_stop)
    jobs = DeferredReconcileJobs()

    assert (
        _tick(state, _auto_config(), daemon, at=_NOW, deferred=jobs, drain=False) == []
    )

    assert [job.label for job in jobs.post_lock] == [f"surface_stop:{_SURFACE}"]
    assert load_state().sessions[0].status is SessionStatus.COMPLETED


def test_act_auto_falls_back_to_flat_backoff_on_unparseable_reset(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail(_UNPARSEABLE_LIMIT_TEXT))
    config = _auto_config(usage_limit_backoff_seconds=_BACKOFF_SECONDS)

    assert _tick(state, config, daemon, at=_NOW) == []
    reverted = _tick(load_state(), config, daemon, at=_LATER)

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

    jobs = DeferredReconcileJobs()
    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config(), deferred=jobs)

    assert reverted == []
    assert daemon.stop_calls == []
    assert jobs.post_lock == []
    assert any(_SID in m and "abandoned" in m for m in _log_messages(caplog))
    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is None
    events_after = len(read_events())

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    assert daemon.stop_calls == []
    _assert_row_still_running()
    assert len(read_events()) == events_after


def test_act_auto_waits_while_surface_still_in_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop() that returned is not trusted: the roster must confirm it.

    The real client swallows every ``claude stop`` failure. The session is
    closed on tick 1, so the row stays RUNNING under its intent while the
    surface is still listed. Tick 2 only re-reads the roster (it queues no
    stop); once the surface is really gone, a later tick finishes the row.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    real_stop = daemon.stop
    # A stop that returns but never takes effect.
    _stop_leaves_worker_listed(monkeypatch, daemon)

    assert _tick(state, _auto_config(), daemon, at=_NOW) == []
    assert daemon.stop_calls == [_SURFACE]
    assert load_state().sessions[0].completed_at == _NOW
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []

    assert any(
        _SID in m and "still in the daemon roster" in m for m in _log_messages(caplog)
    )
    assert daemon.stop_calls == [_SURFACE]
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    # The surface finally leaves the roster (the leaked-worker sweep replays
    # the stop for a terminal session; here the real stop is applied directly).
    monkeypatch.setattr(daemon, "stop", real_stop)
    real_stop(_SURFACE)

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]
    assert _owned_row().status is QueueItemStatus.PENDING
    assert load_state().sessions[0].completed_at == _NOW


def test_leaked_sweep_replays_an_ineffective_stop_until_the_act_finishes(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leaked-worker sweep is what re-queues a stop that did not take (#2549).

    Tick 1 closes the session and its stop leaves the worker listed. The act
    never queues a second stop; tick 2's pre-guard leaked sweep does, once, for
    the now-terminal session. With the real stop restored the replay empties
    the roster and tick 3 requeues the row, charging nothing and keeping the
    close time of tick 1. A sweep that skipped a row carrying the act would
    leave the surface listed for good.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    # list_live_worker_homes fails closed for a live id with no recorded cwd.
    daemon._cwd_by_id[_SURFACE] = tmp_path / "wt-2324"
    real_stop = daemon.stop
    _stop_leaves_worker_listed(monkeypatch, daemon)
    before = _owned_row().unproductive_attempts

    first = DeferredReconcileJobs()
    assert (
        _tick(state, _auto_config(), daemon, at=_NOW, deferred=first, drain=False) == []
    )
    assert [job.label for job in first.post_lock] == [f"surface_stop:{_SURFACE}"]
    run_post_lock_jobs(first)
    assert daemon.stop_calls == [_SURFACE]
    assert load_state().sessions[0].completed_at == _NOW
    assert _owned_row().usage_limit_act is not None

    second = DeferredReconcileJobs()
    assert (
        _tick(
            load_state(),
            _auto_config(),
            daemon,
            at=_LATER,
            deferred=second,
            drain=False,
            leaked_sweep=True,
        )
        == []
    )
    assert [(job.label, job.stop) for job in second.post_lock] == [
        (f"leaked_worker_stop:{_SURFACE}", StopKind.RECOVERABLE)
    ]
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    # The replayed stop now takes effect: the surface leaves the roster.
    monkeypatch.setattr(daemon, "stop", real_stop)
    run_post_lock_jobs(second)
    assert daemon.stop_calls == [_SURFACE, _SURFACE]
    assert daemon.list_live_session_short_ids() == set()

    third = DeferredReconcileJobs()
    assert _tick(
        load_state(),
        _auto_config(),
        daemon,
        at=_LATER,
        deferred=third,
        drain=False,
        leaked_sweep=True,
    ) == [_SID]
    assert third.post_lock == []
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.unproductive_attempts == before
    assert load_state().sessions[0].completed_at == _NOW


def test_act_auto_unreadable_roster_confirms_nothing(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """An unreadable roster cannot confirm the stop, so the act waits (fail closed)."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    daemon.roster_unreadable = True

    assert _tick(state, _auto_config(), daemon, at=_NOW) == []
    assert daemon.stop_calls == [_SURFACE]
    assert load_state().sessions[0].status is SessionStatus.COMPLETED
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
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

    assert _tick(state, _auto_config(), daemon, at=_NOW) == []
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

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

    assert _tick(state, _auto_config(), daemon, at=_NOW) == []
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

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
    real_audit = mid_turn._audit

    def _audit_then_cancel(act: mid_turn._Act) -> bool:
        audited = real_audit(act)
        store = load_dev_queue()
        transition_task_status(store.tasks[0], QueueItemStatus.CANCELLED)
        save_dev_queue(store)
        return audited

    monkeypatch.setattr(mid_turn, "_audit", _audit_then_cancel)
    jobs = DeferredReconcileJobs()

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config(), deferred=jobs)

    assert reverted == []
    assert jobs.post_lock == []
    assert any(_SID in m and "no longer carries" in m for m in _log_messages(caplog))
    _assert_session_still_active(state)
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.CANCELLED
    assert task.usage_limit_act is None
    assert task.next_eligible_at is None


def test_act_leaves_session_open_when_row_is_requeued_mid_act(
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
    real_audit = mid_turn._audit

    def _audit_then_requeue(act: mid_turn._Act) -> bool:
        audited = real_audit(act)
        store = load_dev_queue()
        transition_task_status(store.tasks[0], QueueItemStatus.PENDING)
        store.tasks[0].session_id = None
        save_dev_queue(store)
        return audited

    saves: list[CwState] = []
    monkeypatch.setattr(mid_turn, "_audit", _audit_then_requeue)
    monkeypatch.setattr(mid_turn, "save_state", saves.append)
    jobs = DeferredReconcileJobs()

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        reverted = _act(state, candidates, _auto_config(), deferred=jobs)

    assert reverted == []
    assert jobs.post_lock == []
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

    jobs = DeferredReconcileJobs()

    assert _act(state, candidates, _auto_config(), deferred=jobs) == [_SID]
    assert daemon.stop_calls == []
    assert jobs.post_lock == []
    assert load_state().sessions[0].status is SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Act -- registration slack: a session born after the capture is not "gone" (#2550)
# ---------------------------------------------------------------------------


def _in_flight_auto_act(state: CwState) -> mid_turn._Act:
    """Decide an ``auto`` act on the seeded session; return its resume inputs.

    The surface is absent from the act's ``native_live``, as it is when the
    session was resumed after the lockless roster capture.
    """
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
    return mid_turn._Act(
        row=row,
        state=state,
        session=state.sessions[0],
        native_live=set(),
        now=_NOW,
        deferred=DeferredReconcileJobs(),
        roster_captured_at=_NOW,
    )


def _resume_in_flight(
    state: CwState,
    daemon: FakeNativeDaemonClient,
    *,
    roster_captured_at: datetime | None,
    deferred: DeferredReconcileJobs | None = None,
) -> list[str]:
    """Tick the sweep over an in-flight act whose surface is not in the roster."""
    jobs = DeferredReconcileJobs() if deferred is None else deferred
    with freezegun.freeze_time(_LATER):
        return detect_and_park_mid_turn_usage_limits(
            state,
            now=_LATER,
            native_live=set(),
            config=_auto_config(),
            clients={},
            deferred=jobs,
            roster_captured_at=roster_captured_at,
        )


def test_act_spares_session_resumed_after_the_roster_capture(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ref's absence from the capture proves nothing for a fresh resume.

    The act is in flight (an earlier tick's stop returned RETRY); the worker
    was resumed after the capture, so its new ``surface_ref`` cannot be in the
    snapshot. The session must stay live, the row RUNNING under its intent,
    and nothing stopped.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    state.sessions[0].resumed_at = _NOW + timedelta(seconds=5)
    save_state(state)

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _resume_in_flight(state, daemon, roster_captured_at=_NOW) == []

    assert daemon.stop_calls == []
    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None
    assert any(_SID in m and "postdate" in m for m in _log_messages(caplog))


def test_act_spares_session_resumed_inside_the_registration_slack(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    slack = timedelta(seconds=ROSTER_REGISTRATION_SLACK_SECONDS)
    state.sessions[0].resumed_at = _NOW - slack
    save_state(state)

    assert _resume_in_flight(state, daemon, roster_captured_at=_NOW) == []

    _assert_session_still_active(state)
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None


@pytest.mark.parametrize("captured", ["no_capture_time", "predates_slack"])
def test_act_finishes_when_the_session_is_not_young_enough_to_postdate_the_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    captured: str,
) -> None:
    """Revert check: without the registration-slack spare an absent ref
    still means stopped."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    roster_captured_at = None
    if captured == "predates_slack":
        # Resumed 1 s past the slack window before the capture.
        slack = timedelta(seconds=ROSTER_REGISTRATION_SLACK_SECONDS + 1)
        state.sessions[0].resumed_at = _NOW - slack
        save_state(state)
        roster_captured_at = _NOW
    else:
        state.sessions[0].resumed_at = _NOW + timedelta(seconds=5)
        save_state(state)

    # The worker is really gone: the fresh roster read confirms it, so the
    # act may finish without a stop.
    daemon._live.discard(_SURFACE)

    assert _resume_in_flight(state, daemon, roster_captured_at=roster_captured_at) == [
        _SID
    ]

    assert daemon.stop_calls == []
    assert load_state().sessions[0].status is SessionStatus.COMPLETED
    assert _owned_row().status is QueueItemStatus.PENDING


def test_close_persists_but_waits_when_surface_off_capture_but_still_in_roster_json(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """Off the capture but still listed: close the session, queue nothing, wait.

    The surface is absent from the act's captured roster and the session
    cannot postdate the capture, so the act persists COMPLETED. The fresh
    ``roster.json`` read still lists the surface, so it must not be called
    gone: the row stays RUNNING under its intent and the act queues no stop.
    The leaked-worker sweep then replays the stop; once it lands, the next
    tick requeues the row without a charge.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    # list_live_worker_homes fails closed for a live id with no recorded cwd.
    daemon._cwd_by_id[_SURFACE] = tmp_path / "wt-2324"
    _in_flight_auto_act(state)
    before = _owned_row().unproductive_attempts

    act_jobs = DeferredReconcileJobs()
    assert (
        _resume_in_flight(state, daemon, roster_captured_at=None, deferred=act_jobs)
        == []
    )

    assert act_jobs.post_lock == []
    assert daemon.stop_calls == []
    persisted = load_state().sessions[0]
    assert persisted.status is SessionStatus.COMPLETED
    assert persisted.completed_reason is CompletionReason.USAGE_LIMITED
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None

    # The leaked-worker sweep sees the terminal session's listed surface.
    sweep = DeferredReconcileJobs()
    assert (
        _tick(
            load_state(),
            _auto_config(),
            daemon,
            at=_LATER,
            deferred=sweep,
            drain=False,
            leaked_sweep=True,
        )
        == []
    )
    assert [(job.label, job.stop) for job in sweep.post_lock] == [
        (f"leaked_worker_stop:{_SURFACE}", StopKind.RECOVERABLE)
    ]
    run_post_lock_jobs(sweep)
    assert daemon.stop_calls == [_SURFACE]

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.unproductive_attempts == before


def test_terminal_session_waits_for_the_roster_not_the_capture(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """A closed session is confirmed against a fresh roster read, not the capture.

    The surface is still listed, so the act waits (PENDING). Once the stop has
    taken effect the same act reads the roster as gone and is DONE.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    act = _in_flight_auto_act(state)
    state.sessions[0].resumed_at = _NOW + timedelta(seconds=5)
    state.sessions[0].status = SessionStatus.COMPLETED

    assert mid_turn._close_and_request_stop(act) is mid_turn._Stop.PENDING

    FakeNativeDaemonClient.stop(daemon, _SURFACE)

    assert mid_turn._close_and_request_stop(act) is mid_turn._Stop.DONE


def test_close_is_done_without_a_surface_ref_despite_the_capture_slack(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    act = _in_flight_auto_act(state)
    state.sessions[0].resumed_at = _NOW + timedelta(seconds=5)
    state.sessions[0].surface_ref = None

    assert mid_turn._close_and_request_stop(act) is mid_turn._Stop.DONE
    assert act.deferred.post_lock == []
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
    assert "for the orchestrator to triage" in attention[0]["breadcrumbs"]
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

    assert _tick(state, _auto_config(), daemon, at=_NOW) == []
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]
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


def test_detect_and_park_auto_queues_a_single_stop_across_ticks(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """Tick 2 over a closed session re-reads the roster; it never queues a stop.

    Nothing drains the queue, so the surface stays listed across both ticks.
    Each tick gets its own sink, as in production: a shared sink would dedupe
    a repeat per surface and hide it. The act queues its one stop on tick 1
    and only waits on tick 2 (the leaked-worker sweep owns any replay), and
    its audit events fire once.
    """
    state, _ = _seed(home, tmp_path, _limit_tail())
    first = DeferredReconcileJobs()
    second = DeferredReconcileJobs()

    assert (
        _tick(state, _auto_config(), daemon, at=_NOW, deferred=first, drain=False) == []
    )
    assert (
        _tick(
            load_state(),
            _auto_config(),
            daemon,
            at=_LATER,
            deferred=second,
            drain=False,
        )
        == []
    )

    assert [job.label for job in first.post_lock] == [f"surface_stop:{_SURFACE}"]
    assert second.post_lock == []
    assert len(_events(OrchestratorEventType.USAGE_LIMIT_ARMED)) == 1
    assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None


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
    point: str, monkeypatch: pytest.MonkeyPatch, daemon: FakeNativeDaemonClient
) -> None:
    """Make the act fail once at *point*, the way a crash there would leave it.

    The module's dev-queue writes are the decision, the audit mark and the
    final transition. Under ``park`` all three land on the first tick, in that
    order. Under ``auto`` the first tick stops at the audit mark (the row stays
    RUNNING until the stop is confirmed), so the final transition is a later
    tick's write: ``transition`` therefore matches on state (the save that
    clears the intent) rather than on a cumulative call count (#3395).
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
            "cw.reconcile._shared._reap.record_event",
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
            match=lambda store, *_a, **_k: any(
                t.ticket_id == _SID and t.usage_limit_act is None for t in store.tasks
            ),
        )
    else:  # pragma: no cover - a typo in the parametrize list
        msg = f"unknown injection point {point!r}"
        raise AssertionError(msg)


# The tick (1-based, counting the interrupted first tick) on which an ``auto``
# act requeues its row after a failure at each point (#3395). Confirmed by
# running: a smaller number fails the final ``requeued == [_SID]`` assertion,
# a larger one fails an intermediate ``== []`` assertion.
#
# Under ``auto`` tick N closes the session and queues the stop, and the NEXT
# tick sees the surface gone and requeues, so the requeue never lands on the
# closing tick. A point that fails before the close persists makes tick 2 the
# closing tick and tick 3 the requeue. ``stop``: tick 1 closes and queues, the
# drained stop fails, the test applies it by hand, and tick 2 confirms and
# requeues. ``transition``: tick 1 closes and queues; tick 2 confirms the
# surface gone but its transition save fails; tick 3 requeues.
_AUTO_FINISH_TICK = {
    "decision": 3,
    "lockout": 3,
    "needs-attention": 3,
    "reap-proposed": 3,
    "audit-mark": 3,
    "completed": 3,
    "close": 3,
    "stop": 2,
    "transition": 3,
}
# Under ``park`` ``_resume`` goes straight to ``_finish``, so tick 2 resumes
# from the intent and parks, whatever the point.
_PARK_FINISH_TICK = 2


def _finish_after_failure(
    point: str,
    branch: str,
    config: OrchestratorConfig,
    daemon: FakeNativeDaemonClient,
) -> list[str]:
    """Run the ticks that finish an act whose first tick was interrupted at *point*.

    Each tick between the first and the pinned finishing tick must leave the
    row RUNNING under its act; the finishing tick's result is returned.
    """
    if point == "stop":
        # The failed stop is replayed by the leaked-worker sweep, which this
        # test does not run; apply the stop that sweep would have re-queued.
        FakeNativeDaemonClient.stop(daemon, _SURFACE)
    final = _AUTO_FINISH_TICK[point] if branch == "auto" else _PARK_FINISH_TICK
    for _ in range(2, final):
        assert _tick(load_state(), config, daemon, at=_LATER) == []
        _assert_row_still_running()
        assert _owned_row().usage_limit_act is not None
    return _tick(load_state(), config, daemon, at=_LATER)


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
    _inject(point, monkeypatch, daemon)

    # Under ``auto`` the ``transition`` failure fires on tick 2, not here, so
    # for it this check is trivially true; the intermediate-tick assertions in
    # ``_finish_after_failure`` carry the non-trivial check.
    assert _tick(state, config, daemon, at=_NOW) == []
    _assert_row_still_running()

    requeued = _finish_after_failure(point, branch, config, daemon)

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
        # Closed by the first tick that reached the persist: the stop and the
        # transition fail after it, so their tick already closed the session.
        closed_at = _NOW if point in ("stop", "transition") else _LATER
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

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
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

    assert _tick(load_state(), _auto_config(), daemon, at=after_window) == []
    assert _tick(load_state(), _auto_config(), daemon, at=after_window) == [_SID]

    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.next_eligible_at == _LATER
    assert load_state().sessions[0].status is SessionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Stalled act -- a pending stop that outlives the bound pages once (#3397)
# ---------------------------------------------------------------------------

_BOUND = timedelta(seconds=ACT_STALLED_AFTER_SECONDS)
_STALLED = "usage_limit_act_stalled"


def _stalled_pages() -> list[dict[str, Any]]:
    return [
        p
        for p in _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
        if p["paused_status"] == _STALLED
    ]


def _assert_act_still_open() -> None:
    """Signal only (ADR-0014): the row, its act and the session are untouched."""
    _assert_row_still_running()
    assert _owned_row().usage_limit_act is not None
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_at == _NOW


def _is_stalled_page(event_type: object, payload: object = None, **_k: object) -> bool:
    return (
        event_type is OrchestratorEventType.SESSION_NEEDS_ATTENTION
        and isinstance(payload, dict)
        and payload.get("paused_status") == _STALLED
    )


def _saves_escalation_stamp(store: DevQueueStore, *_a: object, **_k: object) -> bool:
    return any(
        t.ticket_id == _SID
        and t.usage_limit_act is not None
        and t.usage_limit_act.escalated_at is not None
        for t in store.tasks
    )


def test_pending_stop_escalates_once_when_the_act_outlives_the_bound(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    _stop_leaves_worker_listed(monkeypatch, daemon)
    config = _auto_config()

    assert _tick(state, config, daemon, at=_NOW) == []
    attention = _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert [p["paused_status"] for p in attention] == ["usage_limited_mid_turn"]

    just_under = _NOW + _BOUND - timedelta(seconds=1)
    assert _tick(load_state(), config, daemon, at=just_under) == []
    assert _stalled_pages() == []
    pending = _owned_row().usage_limit_act
    assert pending is not None
    assert pending.escalated_at is None

    assert _tick(load_state(), config, daemon, at=_NOW + _BOUND) == []
    pages = _stalled_pages()
    assert len(pages) == 1
    page = pages[0]
    assert page[ACT_STARTED_AT_KEY] == _NOW.isoformat()
    assert page["ticket_id"] == _SID
    assert page["crashed"] is False
    assert page["lane"] == _owned_row().lane
    assert page["breadcrumbs"] == (
        f"act started {int(ACT_STALLED_AFTER_SECONDS)}s ago; stop still pending, "
        f"surface {_SURFACE} still listed or roster unreadable; "
        "row stays RUNNING under its act"
    )
    _assert_act_still_open()
    act = _owned_row().usage_limit_act
    assert act is not None
    assert act.escalated_at == _NOW + _BOUND
    assert daemon.stop_calls == [_SURFACE]

    assert _tick(load_state(), config, daemon, at=_NOW + 2 * _BOUND) == []
    assert len(_stalled_pages()) == 1
    _assert_act_still_open()


def test_unreadable_roster_escalates_too(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that can never be confirmed is the same stall: it pages once."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    _stop_makes_roster_unreadable(monkeypatch, daemon)
    config = _auto_config()

    assert _tick(state, config, daemon, at=_NOW) == []
    assert _tick(load_state(), config, daemon, at=_NOW + _BOUND) == []

    pages = _stalled_pages()
    assert len(pages) == 1
    assert _SURFACE in pages[0]["breadcrumbs"]
    _assert_act_still_open()


def test_escalation_not_emitted_when_act_finishes_even_if_old(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed stop finishes the act; its age alone never pages."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    real_stop = daemon.stop
    _stop_leaves_worker_listed(monkeypatch, daemon)
    config = _auto_config()

    assert _tick(state, config, daemon, at=_NOW) == []
    monkeypatch.setattr(daemon, "stop", real_stop)
    real_stop(_SURFACE)

    assert _tick(load_state(), config, daemon, at=_NOW + 2 * _BOUND) == [_SID]
    assert _stalled_pages() == []
    assert _owned_row().status is QueueItemStatus.PENDING


def test_escalation_emit_failure_retries_next_tick(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed page write leaves the act unstamped; the next tick pages."""
    from cw.events import record_event as real_record_event

    state, _ = _seed(home, tmp_path, _limit_tail())
    _stop_leaves_worker_listed(monkeypatch, daemon)
    config = _auto_config()
    assert _tick(state, config, daemon, at=_NOW) == []
    _fail_once(
        monkeypatch,
        "cw.reconcile.usage_limit_mid_turn.record_event",
        real_record_event,
        match=_is_stalled_page,
    )

    with caplog.at_level("WARNING", logger="cw.reconcile.usage_limit_mid_turn"):
        assert _tick(load_state(), config, daemon, at=_NOW + _BOUND) == []

    assert any(_SID in m and "resumes next tick" in m for m in _log_messages(caplog))
    assert _stalled_pages() == []
    failed = _owned_row().usage_limit_act
    assert failed is not None
    assert failed.escalated_at is None

    later = _NOW + _BOUND + timedelta(minutes=1)
    assert _tick(load_state(), config, daemon, at=later) == []
    assert len(_stalled_pages()) == 1
    stamped = _owned_row().usage_limit_act
    assert stamped is not None
    assert stamped.escalated_at == later
    _assert_act_still_open()


def test_escalation_stamp_failure_repeats_the_page_once(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page whose stamp did not land is re-sent once, then never again."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    _stop_leaves_worker_listed(monkeypatch, daemon)
    config = _auto_config()
    assert _tick(state, config, daemon, at=_NOW) == []
    _fail_once(
        monkeypatch,
        "cw.reconcile.usage_limit_mid_turn.save_dev_queue",
        save_dev_queue,
        match=_saves_escalation_stamp,
    )

    assert _tick(load_state(), config, daemon, at=_NOW + _BOUND) == []
    assert len(_stalled_pages()) == 1
    unstamped = _owned_row().usage_limit_act
    assert unstamped is not None
    assert unstamped.escalated_at is None

    later = _NOW + _BOUND + timedelta(minutes=1)
    assert _tick(load_state(), config, daemon, at=later) == []
    assert len(_stalled_pages()) == 2
    assert _tick(load_state(), config, daemon, at=later + _BOUND) == []
    assert len(_stalled_pages()) == 2
    stamped = _owned_row().usage_limit_act
    assert stamped is not None
    assert stamped.escalated_at == later
    _assert_act_still_open()


def test_park_act_never_emits_the_stalled_page(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    """``park`` has no stop to wait on: it finishes the act on its first tick."""
    state, _ = _seed(home, tmp_path, _limit_tail())

    assert _tick(state, OrchestratorConfig(), daemon, at=_NOW) == []
    assert _tick(load_state(), OrchestratorConfig(), daemon, at=_NOW + _BOUND) == []

    assert _owned_row().status is QueueItemStatus.BLOCKED_ON_USER
    assert _stalled_pages() == []


def test_stalled_status_is_documented() -> None:
    bullet = events_doc_bullet('- `"usage_limit_act_stalled"`')

    assert "escalated_at" in bullet
    assert "ADR-0014" in bullet
    assert "act started {age_seconds}s ago; stop still pending" in bullet


# ---------------------------------------------------------------------------
# Act -- an operator clear makes an act decided before it moot (#3547)
# ---------------------------------------------------------------------------

_MOOT_LINE = "was decided before the operator cleared"


def _clear_mark(at: datetime) -> None:
    from cw.dispatch_state_marks import clear_usage_limited_until

    clear_usage_limited_until(_CLIENT, cleared_at=at)


def _seed_gone_session_act() -> None:
    """A row carrying an ``auto`` act (started at ``_NOW``) whose session is gone."""
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


def test_arm_lockout_skips_a_moot_act(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    act = _in_flight_auto_act(state)
    _clear_mark(_NOW + timedelta(minutes=1))
    saves: list[object] = []
    monkeypatch.setattr(
        "cw.reconcile.usage_limit_mid_turn.merge_and_save_usage_limited_until",
        saves.append,
    )

    with caplog.at_level("INFO", logger="cw.reconcile.usage_limit_mid_turn"):
        assert mid_turn._arm_lockout(act, moot=True) is True

    assert saves == []
    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    assert _lockout() == {}
    assert any(_SID in m and _MOOT_LINE in m for m in _log_messages(caplog))


def test_moot_live_act_requeues_without_stamp_or_arm(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    _clear_mark(_NOW + timedelta(minutes=1))

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert _events(OrchestratorEventType.USAGE_LIMIT_ARMED) == []
    assert _lockout() == {}
    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.next_eligible_at is None


def test_act_started_after_the_mark_arms_and_stamps(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    state, _ = _seed(home, tmp_path, _limit_tail())
    _clear_mark(_NOW - timedelta(minutes=1))
    _in_flight_auto_act(state)

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert [e["client"] for e in _events(OrchestratorEventType.USAGE_LIMIT_ARMED)] == [
        _CLIENT
    ]
    assert _owned_row().next_eligible_at == _RESET_AT


def _audit_then(
    monkeypatch: pytest.MonkeyPatch, side_effect: Callable[[], None]
) -> None:
    real_audit = mid_turn._audit

    def _audit(act: mid_turn._Act) -> bool:
        side_effect()
        return real_audit(act)

    monkeypatch.setattr("cw.reconcile.usage_limit_mid_turn._audit", _audit)


def test_moot_is_computed_once_a_later_mark_does_not_unstamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mark written after the compute, before ``_finish``, is not seen."""
    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    _audit_then(monkeypatch, lambda: _clear_mark(_LATER))

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert _owned_row().next_eligible_at == _RESET_AT


def test_moot_is_computed_once_a_removed_mark_still_unstamps(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cw.dispatch_state

    state, _ = _seed(home, tmp_path, _limit_tail())
    _in_flight_auto_act(state)
    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == []
    _clear_mark(_NOW + timedelta(minutes=1))
    _audit_then(
        monkeypatch,
        lambda: cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{}"),
    )

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert _owned_row().next_eligible_at is None


def test_gone_session_path_honours_the_mark(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    _seed(home, tmp_path, _limit_tail())
    _seed_gone_session_act()
    _clear_mark(_NOW + timedelta(minutes=1))

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    task = _owned_row()
    assert task.status is QueueItemStatus.PENDING
    assert task.next_eligible_at is None


def test_gone_session_path_with_an_older_mark_stamps(
    tmp_config_dir: Path,
    tmp_path: Path,
    home: Path,
    daemon: FakeNativeDaemonClient,
) -> None:
    _seed(home, tmp_path, _limit_tail())
    _seed_gone_session_act()
    _clear_mark(_NOW - timedelta(minutes=1))

    assert _tick(load_state(), _auto_config(), daemon, at=_LATER) == [_SID]

    assert _owned_row().next_eligible_at == _RESET_AT
