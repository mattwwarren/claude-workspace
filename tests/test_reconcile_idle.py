"""Tests for the emitted-sentinel router (``cw.reconcile.idle``).

Since the process-kill-timeout removal the sweep produces exactly one
disposition — ROUTE_EMITTED_SENTINEL (#578) — and transcript quietness never
dispositions a session. These tests pin both the retained routing behavior
and the removal itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import clients_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    CwState,
    DevQueueStore,
    LastResultSource,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile._shared import _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY, ProposedAction
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.idle import (
    _act_on_idle_candidates,
    _detect_idle_candidates,
)
from cw.reconcile.idle import _detect as idle_detect
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
    call_and_drain,
)

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_NOW_PAST_CHECK = _STARTED_AT + timedelta(minutes=10)
_NOW_UNDER_CHECK = _STARTED_AT + timedelta(seconds=100)
_NOW_HOURS_LATER = _STARTED_AT + timedelta(hours=10)


def _shipped_result() -> AutoDevResult:
    return AutoDevResult.model_validate(_shipped_salvage_payload())


@pytest.fixture
def parsed_sentinel(monkeypatch: pytest.MonkeyPatch) -> AutoDevResult:
    """Patch the transcript parse to return a fixed emitted sentinel."""
    result = _shipped_result()
    monkeypatch.setattr(
        idle_detect,
        "_parse_any_sentinel_from_transcript",
        lambda _session: (result, "csid-routed"),
    )
    return result


def _state(tmp_path: Path, *, name: str = "client-a/auto-dev/salv-1") -> CwState:
    sess = _mk_headless_daemon_session("salv-1", tmp_path / "wt", _STARTED_AT)
    sess.name = name
    return CwState(sessions=[sess])


def test_unrouted_sentinel_routes_after_check_delay(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    state = _state(tmp_path)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL
    assert candidate.routed_sentinel is parsed_sentinel
    assert candidate.salvage_csid == "csid-routed"


def test_under_check_delay_no_candidate(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    """The 300 s unrouted check is a re-check delay, not a disposition timer."""
    candidates = _detect_idle_candidates(
        _state(tmp_path),
        now=_NOW_UNDER_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_no_sentinel_never_dispositions_regardless_of_quiet_hours(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal regression: hours of silence with no sentinel → zero candidates.

    Under the removed idle watchdog this session would have been reaped,
    git-salvaged, or parked silently_idle. Now quietness produces nothing.
    """
    monkeypatch.setattr(
        idle_detect, "_parse_any_sentinel_from_transcript", lambda _session: None
    )
    state = _state(tmp_path)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_HOURS_LATER,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []
    assert state.sessions[0].status is SessionStatus.ACTIVE


def test_session_with_last_result_is_skipped(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    state = _state(tmp_path)
    state.sessions[0].last_result = {"paused_status": "anything"}

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_roster_absent_session_is_left_to_the_phantom_sweep(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    candidates = _detect_idle_candidates(
        _state(tmp_path),
        now=_NOW_PAST_CHECK,
        native_live=set(),
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_act_completes_session_on_accepted_route(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    # A name outside the auto-dev/<ticket> shape yields ticket_id=None, so the
    # route is accepted without dev-queue arbitration — the queue-routing arm
    # is covered by the shared _apply_sentinel_to_task tests.
    state = _state(tmp_path, name="client-a/adhoc")
    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    assert len(candidates) == 1

    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.claude_session_id == "csid-routed"
    assert session.last_result is not None
    assert session.last_result["status"] == "shipped"


def test_act_queues_surface_stop_until_the_drain(
    tmp_config_dir: Path,
    tmp_path: Path,
    parsed_sentinel: AutoDevResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1232: the accepted route completes and announces the session in the
    act; the surface stop is only queued, and runs at the post-lock drain."""
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    state = _state(tmp_path, name="client-a/adhoc")
    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    sink = DeferredReconcileJobs()

    _act_on_idle_candidates(state, candidates, now=_NOW_PAST_CHECK, deferred=sink)

    assert state.sessions[0].status is SessionStatus.COMPLETED
    completed = read_events(
        consumer="test-idle-queues-stop",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(completed) == 1
    assert daemon.stop_calls == []
    assert [job.label for job in sink.post_lock] == ["surface_stop:fake-short-id"]

    run_post_lock_jobs(sink)

    assert daemon.stop_calls == ["fake-short-id"]


# ---------------------------------------------------------------------------
# #2458: the idle sweep is the backstop authority for a LIVE session's staged
# ``cw result emit`` result the Stop hook never got to route.
# ---------------------------------------------------------------------------

_EMIT_TICKET = "salv-1"


@pytest.fixture
def no_transcript_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any transcript parse: an emit_cli candidate must come from
    ``session.last_result`` alone."""

    def _fail(_session: object) -> None:
        pytest.fail("emit_cli candidate must not re-parse the transcript")

    monkeypatch.setattr(idle_detect, "_parse_any_sentinel_from_transcript", _fail)


@pytest.fixture
def idle_daemon(monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    daemon = FakeNativeDaemonClient()
    monkeypatch.setattr("cw.reconcile._deps.get_native_daemon_client", lambda: daemon)
    return daemon


def _write_staged_client() -> None:
    path = clients_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "clients:\n"
        "  client-a:\n"
        "    workspace_path: /tmp/ws-idle-2458\n"
        "    default_branch: main\n"
        "    pipeline:\n"
        "      stages: [plan, impl, review, finalize]\n"
    )


def _emit_cli_state(tmp_path: Path, payload: dict[str, object]) -> CwState:
    state = _state(tmp_path)
    session = state.sessions[0]
    session.last_result = payload
    session.last_result_source = LastResultSource.EMIT_CLI
    return state


def _seed_row(status: QueueItemStatus, stage: Stage) -> None:
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_EMIT_TICKET,
                    client="client-a",
                    status=status,
                    session_id="salv-1",
                    stage=stage,
                    attempts=1,
                )
            ]
        )
    )


def _impl_stage_complete() -> dict[str, object]:
    return {**_stage_complete_payload(), "ticket_id": _EMIT_TICKET}


def _reload_row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _EMIT_TICKET)


def test_detect_idle_candidates_routes_live_emit_cli_stage_complete(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """The backstop routes a staged stage_complete and completes the session.

    The idle-sweep twin of the Stop hook's
    ``test_signal_stop_emit_cli_stage_complete_advances_plan_to_impl``.
    """
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL
    assert candidate.ticket_id == _EMIT_TICKET
    assert isinstance(candidate.routed_sentinel, AutoDevResult)
    assert candidate.routed_sentinel.status == "stage_complete"

    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().stage == Stage.REVIEW
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    # The worker's own emitted result is audited, never overwritten.
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_routes_live_emit_cli_shipped(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """The backstop routes a staged terminal ``shipped`` result to COMPLETED.

    The idle-sweep twin of the Stop hook's
    ``test_signal_stop_emit_cli_shipped_completes_finalize_row``: every other
    emit_cli case in this module is non-terminal (``stage_complete``) or a
    park/refusal -- this is the plain terminal completion the ticket's own
    incidents (a ``shipped`` FINALIZE session stuck 35 minutes) needed.
    """
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.FINALIZE)
    payload = _shipped_salvage_payload()
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL
    assert candidate.ticket_id == _EMIT_TICKET
    assert isinstance(candidate.routed_sentinel, AutoDevResult)
    assert candidate.routed_sentinel.status == "shipped"
    # #2458: the candidate's own source, not a hardcoded SALVAGE_TRANSCRIPT.
    assert candidate.result_source is LastResultSource.EMIT_CLI

    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().status == QueueItemStatus.COMPLETED
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    # The worker's own emitted result is audited, never overwritten.
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_routes_live_emit_cli_no_op(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """A staged terminal ``no_op`` result completes the row, mirroring the
    ``shipped`` case above -- comment 2's acceptance bar names ``no_op``
    explicitly among the terminal classes that must be covered. Both statuses
    land on the same ``_apply_sentinel_to_task`` COMPLETED branch, so this
    closes the ticket's literal wording rather than a distinct code path."""
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.FINALIZE)
    payload = {**_no_op_salvage_payload(), "ticket_id": _EMIT_TICKET}
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate.routed_sentinel, AutoDevResult)
    assert candidate.routed_sentinel.status == "no_op"

    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().status == QueueItemStatus.COMPLETED
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_emit_cli_respects_the_unrouted_check_delay(
    tmp_config_dir: Path, tmp_path: Path, no_transcript_parse: None
) -> None:
    """The same ``sentinel_unrouted_check_seconds`` re-check delay applies."""
    state = _emit_cli_state(tmp_path, _impl_stage_complete())

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_UNDER_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def _stamp_background_work(worktree: Path, *, count: int, stamped_at: datetime) -> None:
    """Overwrite the worktree's ``agent_spawn_stamp`` as a deferring Stop does."""
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context[AGENT_SPAWN_STAMP_KEY] = {
        AGENT_SPAWN_UNRESOLVED_COUNT_KEY: count,
        AGENT_SPAWN_LAST_STAMPED_AT_KEY: stamped_at.isoformat(),
    }
    context_path.write_text(json.dumps(context), encoding="utf-8")


def test_idle_sweep_holds_off_staged_emit_while_background_work_drains(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """A ``complete_session=False`` partial route leaves the session ACTIVE,
    still holding its staged result, while ``background_tasks`` drain. Well
    past ``sentinel_unrouted_check_seconds`` from ``started_at``, but with a
    fresh outstanding ``agent_spawn_stamp``, the backstop must not complete
    the session or stop its daemon under the still-running subagent (#151)."""
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    state = _emit_cli_state(tmp_path, _impl_stage_complete())
    _stamp_background_work(
        tmp_path / "wt", count=1, stamped_at=_NOW_PAST_CHECK - timedelta(seconds=30)
    )

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert candidates == []
    assert state.sessions[0].status is SessionStatus.ACTIVE
    assert idle_daemon.stop_calls == []
    assert _reload_row().status == QueueItemStatus.RUNNING


def test_idle_sweep_routes_staged_emit_once_background_stamp_outlives_deadline(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """The dropped-wakeup case (#1889) still routes: an outstanding stamp
    whose last deferring Stop is older than ``fix_loop_await_deadline_minutes``
    means no further Stop is coming, so the backstop routes and completes."""
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    state = _emit_cli_state(tmp_path, _impl_stage_complete())
    config = OrchestratorConfig()
    _stamp_background_work(
        tmp_path / "wt",
        count=1,
        stamped_at=_NOW_HOURS_LATER
        - timedelta(minutes=config.fix_loop_await_deadline_minutes, seconds=1),
    )

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_HOURS_LATER,
        native_live={"fake-short-id"},
        config=config,
        task_by_ticket={},
    )
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_HOURS_LATER)

    assert len(candidates) == 1
    assert _reload_row().stage == Stage.REVIEW
    assert state.sessions[0].status is SessionStatus.COMPLETED
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_skips_emit_cli_already_routed_by_stop_hook(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """No double-route: a row the Stop hook already landed stays untouched.

    The shared #2140 ``task_already_terminal`` carve-out completes the
    session without re-mutating the queue.
    """
    _write_staged_client()
    _seed_row(QueueItemStatus.COMPLETED, Stage.FINALIZE)
    before = _reload_row().model_dump()
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().model_dump() == before
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI


def test_detect_idle_candidates_skips_partial_route_consumed_live_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """#2458 fix cycle 4, Action 1 test 4: no re-arm for a partial-route-
    consumed staged result on a still-live (not yet COMPLETED) session.

    Reconstructs the exact state a #2458 ``complete_session=False`` partial
    route leaves behind while ``background_tasks`` are still draining: the
    session stays ACTIVE (unlike the COMPLETED-row twin above), its task was
    already routed by the Stop hook's own emit-precedence path, and
    ``last_result`` now carries ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY``
    merged in alongside the still terminal-shaped payload. Before this fix,
    ``holds_staged_emit_result`` read EMIT_CLI source + a terminal sentinel
    alone, so this session still qualified as a fresh ROUTE_EMITTED_SENTINEL
    candidate -- a second, race-prone router for a result the Stop hook
    already routed once.
    """
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    before = _reload_row().model_dump()
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)
    state.sessions[0].last_result = {
        **payload,
        _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY: True,
    }

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert candidates == []
    assert _reload_row().model_dump() == before
    assert (
        read_events(
            consumer="t2458-idle-partial-route-consumed",
            event_types=[OrchestratorEventType.SENTINEL_RACE_MISS],
        )
        == []
    )
    session = state.sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert idle_daemon.stop_calls == []


def test_detect_idle_candidates_routes_live_session_with_sentinel_unroutable_paged_flag(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """#2458 fix cycle 6, non-regression: the new Stop-hook paging-dedup flag
    must not gate retry candidacy.

    Distinct from ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY`` (the sibling test
    above, ``test_detect_idle_candidates_skips_partial_route_consumed_
    live_session``): that flag means "this result was already routed" and
    correctly excludes the session from ``holds_staged_emit_result``'s
    candidacy. The new ``cw.cli.stop_hook._SENTINEL_UNROUTABLE_PAGED_KEY``
    (fix cycle 6) means only "the Stop hook's sentinel_unroutable WARNING
    already fired once for this still-unrouted bail" -- routing never
    actually succeeded, so a session carrying it must remain a live
    ``ROUTE_EMITTED_SENTINEL`` candidate for both the idle sweep here and
    ``cw spawn close``'s retry path (``cw.cli.spawn``, the other
    ``holds_staged_emit_result`` consumer). Fixing the double-page bug must
    not silently reintroduce the permanently-stuck-result defect #2458
    exists to fix.
    """
    from cw.cli.stop_hook import _SENTINEL_UNROUTABLE_PAGED_KEY
    from cw.reconcile import holds_staged_emit_result

    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)
    state.sessions[0].last_result = {
        **payload,
        _SENTINEL_UNROUTABLE_PAGED_KEY: True,
    }

    assert holds_staged_emit_result(state.sessions[0]) is True

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL
    assert candidate.ticket_id == _EMIT_TICKET

    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().stage == Stage.REVIEW
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result is not None
    assert session.last_result[_SENTINEL_UNROUTABLE_PAGED_KEY] is True
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_completes_session_for_already_forward_advanced_row(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """Non-final-advance shape (comment 2 / round-2 finding, Action 4).

    The other of the two shapes ``task_already_terminal`` covers: the Stop
    hook already forward-advanced this row past ``cw.dev_queue.lifecycle``'s
    session_id-clearing hop (``lifecycle.py:811``), so the row carries a
    fresh ``session_id=None`` at its NEW stage -- unlike
    ``test_detect_idle_candidates_skips_emit_cli_already_routed_by_stop_hook``,
    which pins the terminal-completion shape (row COMPLETED, session_id
    retained). The session itself still holds the stale non-terminal
    ``stage_complete`` ``last_result`` the Stop hook already consumed. The
    sweep must complete the (now orphaned) session and stop its daemon
    without re-mutating the already-advanced row.
    """
    _write_staged_client()
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_EMIT_TICKET,
                    client="client-a",
                    status=QueueItemStatus.PENDING,
                    session_id=None,
                    stage=Stage.REVIEW,
                    attempts=1,
                )
            ]
        )
    )
    before = _reload_row().model_dump()
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    assert len(candidates) == 1
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().model_dump() == before
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI
    assert idle_daemon.stop_calls == ["fake-short-id"]


def test_detect_idle_candidates_still_skips_transcript_only_when_last_result_is_none(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    """Regression: the pre-existing #578 transcript producer is unchanged."""
    state = _state(tmp_path)
    assert state.sessions[0].last_result is None

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert len(candidates) == 1
    assert candidates[0].routed_sentinel is parsed_sentinel
    assert candidates[0].salvage_csid == "csid-routed"


def test_detect_idle_candidates_skips_terminal_result_not_from_emit_cli(
    tmp_config_dir: Path, tmp_path: Path, parsed_sentinel: AutoDevResult
) -> None:
    """The guards are narrowed, not removed: a terminal ``last_result`` any
    other writer recorded is still the Stop hook's to route."""
    state = _state(tmp_path)
    session = state.sessions[0]
    session.last_result = _impl_stage_complete()
    session.last_result_source = LastResultSource.STOP_HOOK_HARVEST

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_detect_idle_candidates_skips_emit_cli_already_refused(
    tmp_config_dir: Path, tmp_path: Path, no_transcript_parse: None
) -> None:
    """A staged result already refused on a stage mismatch is not re-offered."""
    state = _emit_cli_state(
        tmp_path, {**_impl_stage_complete(), "sentinel_advance_refused": True}
    )

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_emit_cli_stage_mismatch_refusal_stamps_marker_and_stops_reoffering(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """A #1031 refusal leaves the row and live session alone and is not
    re-offered: the #1149 refusal marker replaces the staged result, so the
    session no longer holds a terminal-shaped emit_cli result."""
    _write_staged_client()
    # A stage2_impl report against a row that already advanced to REVIEW.
    _seed_row(QueueItemStatus.RUNNING, Stage.REVIEW)
    before = _reload_row().model_dump()
    payload = _impl_stage_complete()
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    assert len(candidates) == 1
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().model_dump() == before
    session = state.sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}
    assert idle_daemon.stop_calls == []
    assert (
        _detect_idle_candidates(
            state,
            now=_NOW_PAST_CHECK,
            native_live={"fake-short-id"},
            config=OrchestratorConfig(),
            task_by_ticket={},
        )
        == []
    )


def test_detect_idle_candidates_skips_unreconstructable_emit_cli_result(
    tmp_config_dir: Path, tmp_path: Path, no_transcript_parse: None
) -> None:
    """A staged dict that validates as neither union arm carries nothing to
    route -- no candidate, and no transcript re-parse either."""
    state = _emit_cli_state(tmp_path, {"status": "shipped"})

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )

    assert candidates == []


def test_detect_idle_candidates_routes_live_emit_cli_blocked_generic_reason(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """A staged ``blocked`` with an unrecognized reason routes through the
    generic Rule 5 fallback and pages (the ``spike_phase0_failed`` shape)."""
    _write_staged_client()
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    payload = {
        **_impl_stage_complete(),
        "status": "blocked",
        "blocker": {
            "stage": "stage2_impl",
            "reason": "spike_phase0_failed",
            "details": "phase 0 spike failed its acceptance check",
        },
    }
    assert AutoDevResult.model_validate(payload).status == "blocked"
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().status is QueueItemStatus.BLOCKED_ON_USER
    attention = read_events(
        consumer="t2458-idle-blocked",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert [e.payload["paused_status"] for e in attention] == ["blocked"]
    assert attention[0].payload["ticket_id"] == _EMIT_TICKET
    assert state.sessions[0].status is SessionStatus.COMPLETED


def test_detect_idle_candidates_landed_terminal_blocked_tripwire(
    tmp_config_dir: Path,
    tmp_path: Path,
    no_transcript_parse: None,
    idle_daemon: FakeNativeDaemonClient,
) -> None:
    """Tripwire, not a passing-behavior assertion (comment 2 / round-2 finding).

    The idle-sweep twin of the Stop hook's
    ``test_signal_stop_landed_terminal_blocked_stops_daemon_with_bg_tasks_pending``:
    a staged BlockedResult with an unrecognized reason at the attempt cap
    lands the task terminal-FAILED with ``outcome.landed_terminal=True``. The
    Stop hook consumes that flag (``_handle_unrouted_stop``, #1273) and stops
    the daemon; ``_apply_idle_routed_mutations`` does not -- see the
    ``#2458 round 2`` comment on that function. This staging bypasses ``cw
    result emit``'s CLI validation gate directly at the data layer (the CLI
    itself would refuse this bare shape, per
    ``test_result_emit_cli_rejects_bare_blocked_shape_payload``) -- it is a
    tripwire for the day that gate is widened, not a claim this path is
    reachable in production today.

    Recorded, tracked in #2482: the row lands FAILED as expected, but the
    session is left ACTIVE with no daemon stop -- it falls into the
    stage-mismatch-refusal branch instead of completing.
    """
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_EMIT_TICKET,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="salv-1",
                    stage=Stage.PLAN,
                    attempts=3,  # _VALIDATION_FAILED_MAX_ATTEMPTS
                )
            ]
        )
    )
    payload = {
        "status": "blocked",
        "blocker": {
            "stage": "unknown",
            "reason": "unknown_reason_xyz",
            "details": "parser-synthesized blocker",
        },
    }
    state = _emit_cli_state(tmp_path, payload)

    candidates = _detect_idle_candidates(
        state,
        now=_NOW_PAST_CHECK,
        native_live={"fake-short-id"},
        config=OrchestratorConfig(),
        task_by_ticket={},
    )
    assert len(candidates) == 1
    call_and_drain(_act_on_idle_candidates, state, candidates, now=_NOW_PAST_CHECK)

    # Documents the actual (currently-wrong) behavior -- see docstring.
    assert _reload_row().status == QueueItemStatus.FAILED
    session = state.sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert idle_daemon.stop_calls == []
