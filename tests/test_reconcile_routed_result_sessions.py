"""Tests for cw.reconcile.routed_result_sessions (#2524).

A #2458 ``complete_session=False`` partial route routes a session's staged
result (the row advances) but leaves the session ACTIVE until a later Stop
that, once ``background_tasks`` drains, may never fire. Nothing then
completes it: the idle sweep skips it (``holds_staged_emit_result`` is False
once consumed) and the stalled sweep skips EMIT_CLI results (#2435). These
tests pin the signal-only detector and its page-once reconcile sweep.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import save_dev_queue
from cw.events import read_events, record_event
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    CwState,
    DevQueueStore,
    LastResultSource,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import routed_result_sessions
from cw.reconcile.routed_result_sessions import (
    ROUTED_RESULT_STRANDED_REASON,
    StrandedRoutedSession,
    find_stranded_routed_sessions,
    rollback_routed_result_latches,
    session_pins_occupied_row,
    stranded_close_command,
    sweep_routed_result_sessions,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _mk_routed_session,
    _routed_last_result,
    _stage_complete_payload,
    _stamp_transcript_age,
    _state_queue_snapshot,
    _write_agent_spawn_stamp,
)

_LIVE = {"fake-short-id"}
_SID = "2524"


@pytest.fixture
def home() -> Path:
    """Per-test HOME (autouse ``_isolate_home``); transcripts are written here."""
    return Path.home()


@pytest.fixture
def push_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every push-notification call (D1: the sweep must fire none)."""
    calls: list[tuple[str, str]] = []

    def _record(name: str, client: str) -> None:
        calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _record)
    return calls


def _row(
    *,
    status: QueueItemStatus = QueueItemStatus.PENDING,
    stage: Stage = Stage.REVIEW,
    session_id: str | None = None,
    ticket_id: str = _SID,
    lane: str = DEFAULT_LANE,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client="client-a",
        status=status,
        stage=stage,
        session_id=session_id,
        lane=lane,
    )


def _world(
    tmp_path: Path,
    home: Path,
    *,
    stale_minutes: float = 31.0,
    tasks: list[TicketTask] | None = None,
    sid: str = _SID,
    write_transcript: bool = True,
    status: SessionStatus = SessionStatus.ACTIVE,
    last_result: dict[str, object] | None = None,
) -> tuple[CwState, list[TicketTask], datetime]:
    """Persist one stranded-shaped session + queue; return (state, tasks, now).

    Default: the incident shape -- a routed session, 31 minutes stale, its
    ticket's row already advanced to a PENDING review row nothing binds.
    """
    now = datetime.now(UTC)
    worktree = tmp_path / f"wt-{sid}"
    sess = _mk_routed_session(sid, worktree, status=status, last_result=last_result)
    if write_transcript:
        _stamp_transcript_age(home, worktree, stale_minutes=stale_minutes, now=now)
    task_list = [_row()] if tasks is None else tasks
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=task_list))
    return state, task_list, now


def _find(
    state: CwState,
    tasks: list[TicketTask],
    now: datetime,
    *,
    native_live: set[str] | None = None,
    config: OrchestratorConfig | None = None,
) -> list[StrandedRoutedSession]:
    return find_stranded_routed_sessions(
        state,
        tasks,
        now=now,
        native_live=_LIVE if native_live is None else native_live,
        config=config or OrchestratorConfig(),
    )


def _events(event_type: OrchestratorEventType) -> list[dict[str, object]]:
    return [dict(e.payload) for e in read_events(event_types=[event_type])]


class TestFindStrandedRoutedSessions:
    def test_reconcile_client_gate_skips_unconfigured_client(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        assert (
            find_stranded_routed_sessions(
                state,
                tasks,
                now=now,
                native_live=_LIVE,
                config=OrchestratorConfig(),
                enabled_clients={"different-client"},
            )
            == []
        )

    def test_roll_back_page_latch_is_explicit_and_idempotent(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, _tasks, now = _world(tmp_path, home)
        state.sessions[0].reap_proposed_at = now

        assert rollback_routed_result_latches(state, [_SID]) == 1
        assert state.sessions[0].reap_proposed_at is None
        assert rollback_routed_result_latches(state, [_SID]) == 0

    def test_partial_route_consumed_row_pending_stale_30m_is_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        hits = _find(state, tasks, now)

        assert len(hits) == 1
        hit = hits[0]
        assert hit.session.id == _SID
        assert hit.ticket_id == _SID
        assert hit.lane == DEFAULT_LANE
        assert hit.stage is Stage.REVIEW
        assert hit.row_status is QueueItemStatus.PENDING
        assert hit.surface_ref == "fake-short-id"
        assert 30.5 < hit.stale_minutes < 31.5

    @pytest.mark.parametrize(
        "row_status",
        [QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED, None],
    )
    def test_row_completed_cancelled_or_absent_is_found(
        self, tmp_path: Path, home: Path, row_status: QueueItemStatus | None
    ) -> None:
        tasks = [] if row_status is None else [_row(status=row_status)]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)

        hits = _find(state, task_list, now)

        assert len(hits) == 1
        assert hits[0].row_status is row_status
        if row_status is None:
            assert hits[0].stage is DEFAULT_STAGE
            assert hits[0].lane == DEFAULT_LANE

    def test_lane_comes_from_advanced_row(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home, tasks=[_row(lane="review-lane")])

        assert [h.lane for h in _find(state, tasks, now)] == ["review-lane"]

    def test_running_row_bound_to_session_is_never_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        tasks = [_row(status=QueueItemStatus.RUNNING, session_id=_SID)]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)

        assert _find(state, task_list, now) == []

    def test_blocked_on_user_row_pinned_to_session_is_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        tasks = [_row(status=QueueItemStatus.BLOCKED_ON_USER, session_id=_SID)]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)

        assert _find(state, task_list, now) == []

    def test_awaiting_operator_signoff_row_pinned_is_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        tasks = [
            _row(status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, session_id=_SID)
        ]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)

        assert _find(state, task_list, now) == []

    def test_running_row_bound_to_other_session_still_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        """The advanced stage was re-claimed by a fresh session."""
        tasks = [_row(status=QueueItemStatus.RUNNING, session_id="newsess1")]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)

        hits = _find(state, task_list, now)

        assert [h.session.id for h in hits] == [_SID]
        assert hits[0].row_status is QueueItemStatus.RUNNING

    def test_bare_terminal_status_without_consumed_marker_is_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home, last_result={"status": "shipped"})

        assert _find(state, tasks, now) == []

    def test_staged_unrouted_emit_cli_result_is_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        """A staged, still-routable emit_cli result is the idle sweep's job."""
        state, tasks, now = _world(
            tmp_path, home, last_result=_stage_complete_payload()
        )
        assert state.sessions[0].last_result_source is LastResultSource.EMIT_CLI

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize(
        "latch",
        [
            {"sentinel_advance_refused": True},
            {"paused_status": "sentinel_stage_mismatch_refused"},
        ],
    )
    def test_latched_refusal_with_consumed_marker_is_not_found(
        self, tmp_path: Path, home: Path, latch: dict[str, object]
    ) -> None:
        state, tasks, now = _world(
            tmp_path, home, last_result=_routed_last_result(**latch)
        )

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize("marker", [False, "true", 1])
    def test_marker_not_exactly_true_is_not_found(
        self, tmp_path: Path, home: Path, marker: object
    ) -> None:
        state, tasks, now = _world(
            tmp_path,
            home,
            last_result=_routed_last_result(sentinel_partial_route_consumed=marker),
        )

        assert _find(state, tasks, now) == []

    def test_no_last_result_is_not_found(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].last_result = None

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize("park", ["silently_idle", "needs_salvage"])
    def test_park_marker_without_status_is_not_found(
        self, tmp_path: Path, home: Path, park: str
    ) -> None:
        state, tasks, now = _world(tmp_path, home, last_result={"paused_status": park})

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize("stale_minutes", [2.0, 20.0])
    def test_bucket_below_30m_not_found(
        self, tmp_path: Path, home: Path, stale_minutes: float
    ) -> None:
        """LIVE (2m) and stale_15m (20m) are inside the grace window."""
        state, tasks, now = _world(tmp_path, home, stale_minutes=stale_minutes)

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize("stale_minutes", [31.0, 50.0])
    def test_stale_30m_and_stale_45m_found(
        self, tmp_path: Path, home: Path, stale_minutes: float
    ) -> None:
        state, tasks, now = _world(tmp_path, home, stale_minutes=stale_minutes)

        assert len(_find(state, tasks, now)) == 1

    def test_bucket_uses_advanced_rows_stage_floor(
        self, tmp_path: Path, home: Path
    ) -> None:
        """At 31 minutes an IMPL row (floor 35) is LIVE; a REVIEW row is not."""
        state, tasks, now = _world(tmp_path, home, tasks=[_row(stage=Stage.IMPL)])
        assert _find(state, tasks, now) == []

        review_tasks = [_row(stage=Stage.REVIEW)]
        assert len(_find(state, review_tasks, now)) == 1

    def test_unresolved_subagent_spawn_within_deadline_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)
        _write_agent_spawn_stamp(
            tmp_path / f"wt-{_SID}",
            unresolved_count=1,
            stamped_at=now - timedelta(minutes=5),
        )

        assert _find(state, tasks, now) == []

    def test_unresolved_subagent_spawn_past_deadline_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)
        _write_agent_spawn_stamp(
            tmp_path / f"wt-{_SID}",
            unresolved_count=1,
            stamped_at=now - timedelta(minutes=90),
        )

        assert len(_find(state, tasks, now)) == 1

    def test_surface_ref_absent_from_roster_not_found(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        assert _find(state, tasks, now, native_live={"someone-else"}) == []

    def test_null_surface_ref_not_found(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].surface_ref = None

        assert _find(state, tasks, now) == []

    def test_orchestrate_purpose_excluded(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].purpose = SessionPurpose.ORCHESTRATE

        assert _find(state, tasks, now) == []

    def test_user_origin_excluded(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].origin = SessionOrigin.USER

        assert _find(state, tasks, now) == []

    @pytest.mark.parametrize(
        "status",
        [
            SessionStatus.COMPLETED,
            SessionStatus.BACKGROUNDED,
            SessionStatus.TIMED_OUT,
        ],
    )
    def test_completed_backgrounded_timed_out_status_excluded(
        self, tmp_path: Path, home: Path, status: SessionStatus
    ) -> None:
        state, tasks, now = _world(tmp_path, home, status=status)

        assert _find(state, tasks, now) == []

    def test_idle_status_session_found(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home, status=SessionStatus.IDLE)

        assert len(_find(state, tasks, now)) == 1

    def test_session_without_ticket_id_excluded(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].name = "client-a/impl"

        assert _find(state, tasks, now) == []

    def test_missing_transcript_not_found(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home, write_transcript=False)

        assert _find(state, tasks, now) == []

    def test_detect_is_pure(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home)
        record_event(OrchestratorEventType.SESSION_SPAWNED, {"session_id": "x"})
        before = _state_queue_snapshot()

        assert len(_find(state, tasks, now)) == 1

        assert _state_queue_snapshot() == before
        assert state.sessions[0].reap_proposed_at is None


class TestSessionPinsOccupiedRow:
    @pytest.mark.parametrize(
        ("status", "pins"),
        [
            (QueueItemStatus.PENDING, False),
            (QueueItemStatus.COMPLETED, False),
            (QueueItemStatus.CANCELLED, False),
            (QueueItemStatus.RUNNING, True),
            (QueueItemStatus.BLOCKED_ON_USER, True),
            (QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, True),
        ],
    )
    def test_status_decides_pin(self, status: QueueItemStatus, pins: bool) -> None:
        tasks = [_row(status=status, session_id="s1")]

        assert session_pins_occupied_row(tasks, "s1") is pins

    def test_other_sessions_row_does_not_pin(self) -> None:
        tasks = [_row(status=QueueItemStatus.RUNNING, session_id="other")]

        assert session_pins_occupied_row(tasks, "s1") is False

    def test_empty_task_list_does_not_pin(self) -> None:
        assert session_pins_occupied_row([], "s1") is False


class TestStrandedCloseCommand:
    def test_exact_command(self) -> None:
        assert (
            stranded_close_command("abc123") == "cw spawn close --confirmed-dead abc123"
        )


def _sweep(
    state: CwState,
    tasks: list[TicketTask],
    now: datetime,
    *,
    config: OrchestratorConfig | None = None,
) -> list[StrandedRoutedSession]:
    return sweep_routed_result_sessions(
        state,
        now=now,
        native_live=_LIVE,
        config=config or OrchestratorConfig(),
        tasks=tasks,
    )


class TestSweepRoutedResultSessions:
    def test_pages_once_with_exact_close_command(
        self, tmp_path: Path, home: Path, push_calls: list[tuple[str, str]]
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        paged = _sweep(state, tasks, now)

        assert [h.session.id for h in paged] == [_SID]
        attention = _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
        assert len(attention) == 1
        assert attention[0]["paused_status"] == ROUTED_RESULT_STRANDED_REASON
        assert f"cw spawn close --confirmed-dead {_SID}" in str(
            attention[0]["breadcrumbs"]
        )
        assert push_calls == []

        persisted = load_state().sessions[0]
        assert persisted.status is SessionStatus.ACTIVE
        assert persisted.reap_proposed_at is not None
        proposals = _events(OrchestratorEventType.SESSION_REAP_PROPOSED)
        assert len(proposals) == 1
        assert proposals[0]["proposed_action"] == "close_routed_result_session"
        assert proposals[0]["reason"] == "routed_result_stranded"
        assert proposals[0]["lane"] == DEFAULT_LANE
        assert proposals[0]["ticket_id"] == _SID

        assert _sweep(state, tasks, now) == []
        assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
        assert len(_events(OrchestratorEventType.SESSION_REAP_PROPOSED)) == 1

    def test_needs_attention_payload_field_sources(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home, tasks=[_row(lane="review-lane")])
        session = state.sessions[0]
        session.claude_session_id = "csid-2524"

        _sweep(state, tasks, now)

        events = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert len(events) == 1
        assert events[0].correlation_id == _SID
        payload = dict(events[0].payload)
        stale = payload.pop("stale_minutes")
        assert isinstance(stale, float)
        assert 30.5 < stale < 31.5
        breadcrumbs = payload.pop("breadcrumbs")
        assert "review/pending" in str(breadcrumbs)
        assert payload == {
            "session_id": session.id,
            "session_name": session.name,
            "client": "client-a",
            "ticket_id": _SID,
            "claude_session_id": "csid-2524",
            "paused_status": ROUTED_RESULT_STRANDED_REASON,
            "crashed": False,
            "stage": "review",
            "lane": "review-lane",
        }

    def test_breadcrumbs_name_missing_row(self, tmp_path: Path, home: Path) -> None:
        state, tasks, now = _world(tmp_path, home, tasks=[])

        _sweep(state, tasks, now)

        attention = _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
        assert "no queue row" in str(attention[0]["breadcrumbs"])
        assert attention[0]["stage"] == DEFAULT_STAGE.value

    def test_page_write_failure_leaves_session_unstamped_and_retries(
        self,
        tmp_path: Path,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr(routed_result_sessions, "record_event", _boom)
        assert _sweep(state, tasks, now) == []
        assert state.sessions[0].reap_proposed_at is None
        assert _events(OrchestratorEventType.SESSION_REAP_PROPOSED) == []
        assert "inbox unwritable" in caplog.text

        monkeypatch.setattr(routed_result_sessions, "record_event", record_event)
        assert [h.session.id for h in _sweep(state, tasks, now)] == [_SID]
        assert state.sessions[0].reap_proposed_at is not None

    def test_proposal_write_failure_does_not_raise(
        self,
        tmp_path: Path,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An OSError from the SESSION_REAP_PROPOSED write must not abort the
        rest of the reconcile tick; the page already landed."""
        state, tasks, now = _world(tmp_path, home)

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "proposal write failed"
            raise OSError(msg)

        monkeypatch.setattr("cw.reconcile._shared.record_event", _boom)

        paged = _sweep(state, tasks, now)

        assert [h.session.id for h in paged] == [_SID]
        assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1
        assert "proposal write failed" in caplog.text

    @pytest.mark.parametrize("policy", ["signal_only", "global_auto", "lane_auto"])
    def test_every_reap_policy_only_pages_never_closes(
        self,
        tmp_path: Path,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        policy: str,
    ) -> None:
        from cw.config import dev_queue_file

        config = _auto_config() if policy == "global_auto" else OrchestratorConfig()
        if policy == "lane_auto":
            monkeypatch.setattr(
                "cw.reconcile._deps.load_effective_clients",
                lambda: {
                    "client-a": _client_with_lane(
                        "client-a", DEFAULT_LANE, reap_policy=ReapPolicy.AUTO
                    )
                },
            )
        state, tasks, now = _world(tmp_path, home)
        queue_before = dev_queue_file().read_bytes()

        paged = _sweep(state, tasks, now, config=config)

        assert len(paged) == 1
        persisted = load_state().sessions[0]
        assert persisted.status is SessionStatus.ACTIVE
        assert persisted.completed_reason is None
        assert persisted.reap_reason is None
        assert persisted.reap_proposed_at is not None
        assert dev_queue_file().read_bytes() == queue_before
        assert len(_events(OrchestratorEventType.SESSION_NEEDS_ATTENTION)) == 1

    def test_sweep_emits_no_session_completed_or_reap_authorized_event(
        self, tmp_path: Path, home: Path
    ) -> None:
        state, tasks, now = _world(tmp_path, home)

        _sweep(state, tasks, now)

        assert _events(OrchestratorEventType.SESSION_COMPLETED) == []
        assert _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED) == []

    def test_running_row_bound_session_untouched_and_unpaged(
        self, tmp_path: Path, home: Path
    ) -> None:
        tasks = [_row(status=QueueItemStatus.RUNNING, session_id=_SID)]
        state, task_list, now = _world(tmp_path, home, tasks=tasks)
        before = _state_queue_snapshot()

        assert _sweep(state, task_list, now) == []

        assert _state_queue_snapshot() == before
        assert state.sessions[0].reap_proposed_at is None

    def test_sweep_noop_when_nothing_stranded(
        self, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, tasks, now = _world(tmp_path, home, stale_minutes=2.0)
        saves: list[CwState] = []
        monkeypatch.setattr("cw.reconcile._shared.save_state", saves.append)

        assert _sweep(state, tasks, now) == []

        assert saves == []
        assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []
        assert _events(OrchestratorEventType.SESSION_REAP_PROPOSED) == []

    def test_already_proposed_session_is_not_paged_again(
        self, tmp_path: Path, home: Path
    ) -> None:
        """Another proposal already stamped reap_proposed_at: no second page."""
        state, tasks, now = _world(tmp_path, home)
        state.sessions[0].reap_proposed_at = now - timedelta(hours=1)

        assert _sweep(state, tasks, now) == []
        assert _events(OrchestratorEventType.SESSION_NEEDS_ATTENTION) == []

    def test_module_has_no_daemon_stop_subprocess_or_lock_reference(self) -> None:
        """R3 + D1 regression guard: the sweep module never reaches for a
        daemon stop, a subprocess, gh, sessions_lock, a thread, or a push."""
        tree = ast.parse(inspect.getsource(routed_result_sessions))
        forbidden = {
            "subprocess",
            "native_daemon",
            "sessions_lock",
            "fire_push_notification",
            "threading",
            "gh",
        }
        names: set[str] = set()
        stop_calls: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(part for a in node.names for part in a.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                names.update((node.module or "").split("."))
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
                if node.attr == "stop":
                    stop_calls.append(node.lineno)

        assert names & forbidden == set()
        assert stop_calls == []
