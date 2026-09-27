"""Tests for the emitted-sentinel router (``cw.reconcile.idle``).

Since the process-kill-timeout removal the sweep produces exactly one
disposition — ROUTE_EMITTED_SENTINEL (#578) — and transcript quietness never
dispositions a session. These tests pin both the retained routing behavior
and the removal itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import clients_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
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
from cw.reconcile._shared import ProposedAction
from cw.reconcile.idle import (
    _act_on_idle_candidates,
    _detect_idle_candidates,
)
from cw.reconcile.idle import _detect as idle_detect
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
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

    _act_on_idle_candidates(state, candidates, now=_NOW_PAST_CHECK)

    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.claude_session_id == "csid-routed"
    assert session.last_result is not None
    assert session.last_result["status"] == "shipped"


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

    _act_on_idle_candidates(state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().stage == Stage.REVIEW
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    # The worker's own emitted result is audited, never overwritten.
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
    _act_on_idle_candidates(state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().model_dump() == before
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result == payload
    assert session.last_result_source is LastResultSource.EMIT_CLI


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
    _act_on_idle_candidates(state, candidates, now=_NOW_PAST_CHECK)

    assert _reload_row().status is QueueItemStatus.BLOCKED_ON_USER
    attention = read_events(
        consumer="t2458-idle-blocked",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert [e.payload["paused_status"] for e in attention] == ["blocked"]
    assert attention[0].payload["ticket_id"] == _EMIT_TICKET
    assert state.sessions[0].status is SessionStatus.COMPLETED
