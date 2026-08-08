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
from cw.models import CwState, OrchestratorConfig, SessionStatus
from cw.reconcile._shared import ProposedAction
from cw.reconcile.idle import (
    _act_on_idle_candidates,
    _detect_idle_candidates,
)
from cw.reconcile.idle import _detect as idle_detect
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
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
