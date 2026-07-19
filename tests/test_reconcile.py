"""Unit tests for cw.reconcile."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import freezegun
import pytest

from cw._util import claude_project_dir
from cw.auto_dev_result import (
    BLOCKER_REASON_VALIDATION_FAILED,
    AutoDevResult,
    BlockedResult,
    Blocker,
    parse_stdout,
)
from cw.config import (
    load_state,
    orchestrator_config_file,
    save_state,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    DEFAULT_LANE,
    ClientConfig,
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import (
    _SALVAGE_SKIP_REASON,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    HEADLESS_TIMEOUT_SECONDS,
    IDLE_WATCHDOG_SECONDS,
    SPAWN_GRACE_SECONDS,
    SentinelRouteOutcome,
    _apply_sentinel_to_task,
    _claude_agents_json,
    _has_terminal_sentinel,
    compute_drift,
    flag_silently_idle_daemon_sessions,
    reconcile,
    resolve_headless_budget,
    revert_completed_silent_tasks,
    revert_stalled_headless_sessions,
    revert_timed_out_tasks,
)
from cw.reconcile._shared import _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
from cw.reconcile.gate_recipes import (
    RECIPE_AUTO_ADOPT_PLAN,
    RECIPE_AUTO_APPROVE_REVIEW,
)
from tests._reconcile_helpers import (
    _auto_config,
    _client_with_lane,
    _make_terminal_payload,
    _mk_daemon_session_with_worktree,
    _mk_headless_daemon_session,
    _mk_live_idle_daemon_session,
    _mk_phantom_daemon_session,
    _mk_session,
    _no_op_salvage_payload,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _state_queue_snapshot,
    _write_idle_transcript_with_text,
    _write_salvage_transcript,
    _write_staged_clients_yaml,
    _write_transcript_records,
)
from tests.conftest import (
    _make_daemon_session,
    _write_idle_transcript,
    plan_body,
    stub_fetch_plan,
)
from tests.test_reconcile_gate_recipes import (
    _clean_result,
    _make_session,
    _plan_result,
    _write_acme_clients_yaml,
)


def test_claude_agents_json_parses_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json parses the subprocess output and returns a list."""
    import json as _json

    fake_output = _json.dumps([{"sessionId": "abc12345"}, {"sessionId": "def67890"}])

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            stdout = fake_output
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
    result = _claude_agents_json()
    assert result == [{"sessionId": "abc12345"}, {"sessionId": "def67890"}]


def test_claude_agents_json_returns_empty_on_non_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json returns [] when daemon output is not a list."""
    import json as _json

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        class _Result:
            stdout = _json.dumps({"error": "not a list"})
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
    result = _claude_agents_json()
    assert result == []


def test_claude_agents_json_passes_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_claude_agents_json passes a timeout= kwarg to subprocess.run (#1230).

    An unbounded `claude agents --json` call under sessions_lock wedges the
    fleet on any CLI hang — the timeout is the guard against that.
    """
    captured_kwargs: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        captured_kwargs.update(kwargs)

        class _Result:
            stdout = "[]"
            returncode = 0

        return _Result()

    monkeypatch.setattr("cw.reconcile._shared.subprocess.run", _fake_run)
    _claude_agents_json()
    assert "timeout" in captured_kwargs
    assert captured_kwargs["timeout"] == 15


def test_compute_drift_empty_state_returns_empty_report() -> None:
    state = CwState()
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_flags_active_session_with_missing_surface() -> None:
    state = CwState(sessions=[_mk_session("s1", "missing-ref")])
    report = compute_drift(state, set())
    assert report.phantom_session_ids == ["s1"]


def test_compute_drift_ignores_backgrounded_completed_and_refless() -> None:
    state = CwState(
        sessions=[
            _mk_session("s-bg", "ref1", status=SessionStatus.BACKGROUNDED),
            _mk_session("s-done", "ref2", status=SessionStatus.COMPLETED),
            _mk_session("s-noref", None, status=SessionStatus.ACTIVE),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_respects_live_set() -> None:
    live_ref = "live-short-id"
    state = CwState(
        sessions=[
            _mk_session("alive", live_ref),
            _mk_session("dead", "gone"),
        ]
    )
    report = compute_drift(state, {live_ref})
    assert report.phantom_session_ids == ["dead"]


def test_compute_drift_native_daemon_live_set_counts_as_alive() -> None:
    """A surface_ref present in the native daemon's roster is not phantom.

    Daemon-origin workers spawned via ``claude --bg`` store the short
    Claude session id as ``surface_ref``. Reconcile must consider them
    alive via the native roster even though no multiplexer adapter is used.
    """
    daemon = FakeNativeDaemonClient()
    native_ref = daemon.spawn_bg(cwd=Path("/tmp"), prompt="x")
    native_live = {native_ref}
    state = CwState(
        sessions=[
            _mk_session("native-alive", native_ref),
            _mk_session("native-dead", "no-such-short-id"),
        ]
    )
    report = compute_drift(state, native_live)
    assert report.phantom_session_ids == ["native-dead"]


def test_compute_drift_spawn_grace_window_protects_fresh_sessions() -> None:
    """Sessions younger than SPAWN_GRACE_SECONDS are not reaped as phantom.

    Regression test for #271: ``claude --bg`` spawn → daemon roster
    registration is async. A reconcile call in the same dispatch tick as
    the spawn would otherwise see the not-yet-registered session as a
    phantom and reap it within 1 second. Real-world latency observed
    2026-05-26: 0.3-1.5s between spawn and roster registration.
    """
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    fresh = _mk_session("fresh", "fresh-ref", started_at=now - timedelta(seconds=2))
    old = _mk_session("old", "old-ref", started_at=now - timedelta(seconds=120))
    state = CwState(sessions=[fresh, old])

    # native_live is empty (both refs missing from daemon roster)
    report = compute_drift(state, set(), now=now)

    # Only the old one is reaped; the fresh one is in the grace window.
    assert report.phantom_session_ids == ["old"]


def test_compute_drift_grace_expires_after_spawn_grace_seconds() -> None:
    """A session just past SPAWN_GRACE_SECONDS is eligible for phantom-reaping."""
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    just_expired = _mk_session(
        "expired",
        "expired-ref",
        started_at=now - timedelta(seconds=SPAWN_GRACE_SECONDS + 1),
    )
    state = CwState(sessions=[just_expired])
    report = compute_drift(state, set(), now=now)
    assert report.phantom_session_ids == ["expired"]


def test_compute_drift_empty_live_set_from_both_backends_is_reconciled() -> None:
    """Empty live set: every ACTIVE/IDLE session with a surface_ref is
    phantom. The reconciler trusts the backend; callers who want
    "don't touch state when daemon is down" must guard before calling.
    """
    state = CwState(
        sessions=[
            _mk_session("s1", "r1"),
            _mk_session("s2", "r2", status=SessionStatus.IDLE),
        ]
    )
    report = compute_drift(state, set())
    assert set(report.phantom_session_ids) == {"s1", "s2"}


def test_compute_drift_skips_orchestrate_purpose_session() -> None:
    """ORCHESTRATE sessions are excluded from phantom detection (R5 guard).

    ORCHESTRATE binding sessions have no live worker process by design,
    so they must never be classified as phantoms.
    """
    state = CwState(
        sessions=[
            _mk_session("orch1", "missing-ref", purpose=SessionPurpose.ORCHESTRATE),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == []


def test_compute_drift_still_flags_impl_phantom_with_orchestrate_present() -> None:
    """ORCHESTRATE guard does not suppress IMPL phantom detection."""
    state = CwState(
        sessions=[
            _mk_session("orch1", "missing-ref", purpose=SessionPurpose.ORCHESTRATE),
            _mk_session("impl1", "also-missing"),
        ]
    )
    report = compute_drift(state, set())
    assert report.phantom_session_ids == ["impl1"]


# ---------------------------------------------------------------------------
# revert_stalled_headless_sessions tests (GitHub issue #185)
# ---------------------------------------------------------------------------


def test_resolve_headless_budget_small_tier(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-tier default: session with scope.tier='small' → 1800s."""
    worktree = tmp_path / "wt-small"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="small-tier-sess",
        name="client-a/auto-dev/GEN-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "small"}},
    )
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 1800


def test_resolve_headless_budget_large_tier(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-tier default: session with scope.tier='large' → 5400s."""
    worktree = tmp_path / "wt-large"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="large-tier-sess",
        name="client-a/auto-dev/GEN-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "large"}},
    )
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 5400


def test_resolve_headless_budget_per_ticket_override(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Per-ticket override beats tier: headless_timeout_override=7200 > small=1800."""
    worktree = tmp_path / "wt-override"
    worktree.mkdir(parents=True, exist_ok=True)

    sess = Session(
        id="override-sess",
        name="client-a/auto-dev/GEN-3",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result={"scope": {"tier": "small"}},
    )
    task = TicketTask(
        ticket_id="GEN-3",
        client="client-a",
        headless_timeout_override=7200,
    )
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 7200


def test_resolve_headless_budget_pre_stage1_fallback(
    tmp_config_dir: Path,
) -> None:
    """Pre-Stage-1 fallback: no task, no last_result → HEADLESS_TIMEOUT_SECONDS."""
    sess = Session(
        id="fallback-sess",
        name="client-a/auto-dev/GEN-4",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_result=None,
    )
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    budget = resolve_headless_budget(None, sess, config)
    assert budget == HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_scope_hint_large_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint='large' + session=None → large-tier budget."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    budget = resolve_headless_budget(task, None, config)
    assert budget == 5400
    assert budget != HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_scope_hint_small_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint='small' + session=None → small-tier budget."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="small")
    budget = resolve_headless_budget(task, None, config)
    assert budget == 1800


def test_resolve_headless_budget_no_scope_hint_no_session(
    tmp_config_dir: Path,
) -> None:
    """Step 2.5 (#314): scope_hint=None + session=None → global timeout."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a")
    budget = resolve_headless_budget(task, None, config)
    assert budget == HEADLESS_TIMEOUT_SECONDS


def test_resolve_headless_budget_override_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Step 1 (override) beats step 2.5 (scope_hint): override=9999 > large=5400."""
    config = _auto_config(headless_timeout_by_tier={"small": 1800, "large": 5400})
    task = TicketTask(
        ticket_id="GEN-314",
        client="client-a",
        scope_hint="large",
        headless_timeout_override=9999,
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 9999


def test_resolve_headless_budget_last_result_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Step 2 (last_result tier) beats step 2.5 (scope_hint) when tier is present."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    sess = Session(
        name="client-a/auto-dev/GEN-314",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "small"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 1800  # small from last_result, not large from scope_hint


def test_resolve_headless_budget_non_dict_last_result_falls_to_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Non-dict last_result → AttributeError caught → step 2.5 scope_hint fires."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={},
    )
    task = TicketTask(ticket_id="GEN-314", client="client-a", scope_hint="large")
    sess = Session(
        name="client-a/auto-dev/GEN-314",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
    )
    sess.last_result = ["not", "a", "dict"]  # type: ignore[assignment]
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 5400  # scope_hint fires after AttributeError caught


def test_resolve_headless_budget_per_stage_hit_beats_tier(
    tmp_config_dir: Path,
) -> None:
    """Per-stage REVIEW default (7200) beats the small-tier default (1800)."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.REVIEW: 7200},
    )
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.REVIEW)
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "small"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 7200


def test_resolve_headless_budget_per_stage_hit_beats_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Per-stage PLAN default (3600) beats the large-tier scope_hint (5400)."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.PLAN,
        scope_hint="large",
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 3600


def test_resolve_headless_budget_per_stage_hit_beats_global(
    tmp_config_dir: Path,
) -> None:
    """Per-stage IMPL default (4200) beats the global HEADLESS_TIMEOUT_SECONDS."""
    config = _auto_config(headless_timeout_by_stage={Stage.IMPL: 4200})
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.IMPL)
    budget = resolve_headless_budget(task, None, config)
    assert budget == 4200


def test_resolve_headless_budget_per_stage_override_still_beats_stage(
    tmp_config_dir: Path,
) -> None:
    """Step 1 (headless_timeout_override) still outranks step 1.5 (per-stage)."""
    config = _auto_config(headless_timeout_by_stage={Stage.REVIEW: 7200})
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.REVIEW,
        headless_timeout_override=9999,
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 9999


def test_resolve_headless_budget_stage_absent_falls_through_to_tier(
    tmp_config_dir: Path,
) -> None:
    """Stage absent from the per-stage map (HARDEN) falls through to tier."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(ticket_id="GEN-1020", client="client-a", stage=Stage.HARDEN)
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "large"}},
    )
    budget = resolve_headless_budget(task, sess, config)
    assert budget == 5400


def test_resolve_headless_budget_stage_absent_falls_through_to_scope_hint(
    tmp_config_dir: Path,
) -> None:
    """Stage absent from the per-stage map (HARDEN) falls through to scope_hint."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    task = TicketTask(
        ticket_id="GEN-1020",
        client="client-a",
        stage=Stage.HARDEN,
        scope_hint="small",
    )
    budget = resolve_headless_budget(task, None, config)
    assert budget == 1800


def test_resolve_headless_budget_task_none_skips_stage_step(
    tmp_config_dir: Path,
) -> None:
    """task=None short-circuits step 1.5 exactly as it already short-circuits step 1."""
    config = _auto_config(
        headless_timeout_by_tier={"small": 1800, "large": 5400},
        headless_timeout_by_stage={Stage.PLAN: 3600},
    )
    sess = Session(
        name="client-a/auto-dev/GEN-1020",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        workspace_path=Path("/tmp/ws"),
        last_result={"scope": {"tier": "large"}},
    )
    budget = resolve_headless_budget(None, sess, config)
    assert budget == 5400


def test_has_terminal_sentinel_unit(
    tmp_config_dir: Path,
) -> None:
    """_has_terminal_sentinel returns True only for dicts with a 'status' key."""
    base = Session(
        id="unit-sentinel",
        name="client-a/unit-sentinel",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    base.last_result = {"status": "shipped"}
    assert _has_terminal_sentinel(base) is True

    base.last_result = {"status": "blocked", "blocker": {"reason": "impl_failed"}}
    assert _has_terminal_sentinel(base) is True

    base.last_result = {"paused_status": "silently_idle"}
    assert _has_terminal_sentinel(base) is False

    base.last_result = {"paused_status": "needs_salvage"}
    assert _has_terminal_sentinel(base) is False

    base.last_result = None
    assert _has_terminal_sentinel(base) is False

    base.last_result = "not-a-dict"  # type: ignore[assignment]
    assert _has_terminal_sentinel(base) is False


def test_awaiting_subagent_true_when_tail_is_pending_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Last assistant turn is a tool_use with no tool_result yet → awaiting."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    tu_ts = "2026-01-01T00:04:00Z"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": tu_ts,
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_false_when_tool_result_delivered(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """tool_use followed by tool_result → NOT awaiting (genuine hang)."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    lines = [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:04:00Z",
            "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Agent"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:04:01Z",
            "message": {"content": [{"type": "tool_result"}]},
        },
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_false_when_pending_tool_use_too_old(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use older than SUBAGENT_LIVENESS_WINDOW → hung subagent."""
    from cw.reconcile import SUBAGENT_LIVENESS_WINDOW_SECONDS, _awaiting_subagent

    # Window is 1800 s (30 min) as of #544; tool_use exactly at the boundary
    # (1800 s old) is expired (< is strict).
    assert SUBAGENT_LIVENESS_WINDOW_SECONDS == 1800
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:30:00Z",  # exactly 1800 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_true_for_20_min_old_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use ~20 min old is within the 1800 s window → alive (#544).

    A large refactor running a single tool call for 20-30 min must NOT be reaped.
    The liveness window was raised from 900→1800 s specifically to cover this case.
    """
    from cw.reconcile import _awaiting_subagent

    # now=01:00:00, tool_use at 00:40:00 → 20 min = 1200 s < 1800 → alive
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:40:00Z",  # 20 min = 1200 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_false_for_35_min_old_tool_use(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pending tool_use ~35 min old is beyond the 1800 s window → reaped (#544).

    The guard must not become permanent: a tool_use older than 1800 s indicates
    a genuinely hung subagent and the watchdog should still reap it.
    """
    from cw.reconcile import _awaiting_subagent

    # now=01:00:00, tool_use at 00:25:00 → 35 min = 2100 s > 1800 → expired
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:25:00Z",  # 35 min = 2100 s before now
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


# ---------------------------------------------------------------------------
# Task A2: watchdog skips workers awaiting a subagent
# ---------------------------------------------------------------------------


def test_detect_usage_limit_returns_true_when_message_present(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns True for a usage-limit phrase (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-ul-direct"
    sess = _mk_headless_daemon_session("ul-direct", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "You've hit your session limit · resets 5:20pm"
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    assert _detect_usage_limit(sess) is True


def test_detect_usage_limit_returns_false_when_absent(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False for a normal assistant message (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-no-ul"
    sess = _mk_headless_daemon_session("no-ul", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "Here is my analysis of the task."
    )
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    assert _detect_usage_limit(sess) is False


def test_detect_usage_limit_returns_false_for_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False when transcript predates started_at (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)  # session started at 01:00
    worktree = tmp_path / "wt-stale-ul"
    sess = _mk_headless_daemon_session("stale-ul", worktree, started_at)

    transcript = _write_idle_transcript_with_text(
        home, worktree, "You've hit your session limit · resets 5:20pm"
    )
    # Stamp BEFORE started_at so the stale-transcript guard fires.
    before_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (before_ts, before_ts))

    assert _detect_usage_limit(sess) is False


def test_detect_usage_limit_returns_false_when_no_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_detect_usage_limit returns False when no transcript exists (#486)."""
    from cw.reconcile import _detect_usage_limit

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-no-trans"
    sess = _mk_headless_daemon_session("no-trans", worktree, started_at)
    # Do NOT write any transcript — project dir doesn't exist either.

    assert _detect_usage_limit(sess) is False


def test_awaiting_subagent_skips_blank_lines_and_bad_json(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Blank lines and malformed JSON in transcript are skipped gracefully."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    # blank line, bad JSON, then valid tool_use with no result
    transcript.write_text(
        "\n"
        "not-json\n"
        + json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:04:00Z",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is True


def test_awaiting_subagent_skips_non_list_content(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Entries with non-list content field are skipped without error."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    # entry with non-list content — should be skipped, not crash
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:04:00Z",
                "message": {"content": "not-a-list"},
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        # Nothing to track — returns False (no pending tool_use)
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_handles_invalid_timestamp(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Invalid ISO timestamp on tool_use → last_tool_use_ts stays None → False."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    transcript = project_dir / "sess-uuid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-valid-timestamp",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Agent"}],
                },
            }
        )
        + "\n"
    )
    sess = _make_daemon_session(claude_session_id="sess-uuid")
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        # invalid ts → last_tool_use_ts = None → returns False
        assert _awaiting_subagent(sess, now) is False


def test_awaiting_subagent_returns_false_on_oserror(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """OSError while reading transcript → fail-open False."""
    from cw.reconcile import _awaiting_subagent

    now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # Point at a file that does not exist
    sess = _make_daemon_session(claude_session_id="no-such-file")
    # project_dir exists but the specific .jsonl doesn't
    with patch("cw.reconcile._shared._session_project_dir", return_value=project_dir):
        assert _awaiting_subagent(sess, now) is False


def test_reconcile_tolerates_malformed_json_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json.JSONDecodeError from _claude_agents_json → daemon_errored semantics.

    reconcile() must NOT raise; with live ACTIVE sessions present the outage
    guard fires and state is left unchanged (#432).
    """
    state = CwState(
        sessions=[
            _mk_session("s-json", "ref-json"),
        ]
    )
    save_state(state)

    def _bad_json() -> list[dict[str, object]]:
        msg = "bad json"
        raise json.JSONDecodeError(msg, "", 0)

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _bad_json)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-json")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


def test_reconcile_tolerates_file_not_found_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileNotFoundError (claude not on PATH) → daemon_errored semantics.

    reconcile() must NOT raise; with live ACTIVE sessions present the outage
    guard fires and state is left unchanged (#432).
    """
    state = CwState(
        sessions=[
            _mk_session("s-fnf", "ref-fnf"),
        ]
    )
    save_state(state)

    def _no_binary() -> list[dict[str, object]]:
        msg = "No such file or directory: 'claude'"
        raise FileNotFoundError(msg)

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _no_binary)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-fnf")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


def test_reconcile_tolerates_timeout_from_claude_agents(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess.TimeoutExpired from _claude_agents_json → daemon_errored semantics.

    A `claude agents --json` hang must not wedge sessions_lock or trigger
    mass-reaping. reconcile() must NOT raise; with live ACTIVE sessions
    present the outage guard fires and state is left unchanged (#1230).
    """
    state = CwState(
        sessions=[
            _mk_session("s-timeout", "ref-timeout"),
        ]
    )
    save_state(state)

    def _hangs() -> list[dict[str, object]]:
        raise subprocess.TimeoutExpired(cmd=["claude", "agents", "--json"], timeout=15)

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _hangs)

    # Must not raise
    report = reconcile()

    assert report.phantom_session_ids == []
    assert report.phantom_session_names == []
    # State must be unchanged — no session reaped, no reap/park mutation
    reloaded = load_state()
    s = reloaded.find_by_name_or_id("s-timeout")
    assert s is not None
    assert s.status == SessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# GitHub issue #431: salvage all terminal-no-retry statuses + skip parked sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "scope_exceeded",
        "forbidden_area",
        "merge_gate_blocked",
    ],
)
def test_salvage_all_terminal_statuses_from_phantom(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom session whose transcript emits each non-retry terminal status must be
    salvaged (COMPLETED), NOT reverted to PENDING (#431).

    Covers the bug where _SALVAGE_TERMINAL_STATUSES was {"shipped", "no_op"} and
    missed scope_exceeded, forbidden_area, plan_pending_approval,
    review_pending_approval, merge_gate_blocked, and the PAUSED_FOR_USER_INPUT
    statuses.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"431-{status}"
    worktree = tmp_path / f"wt-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under headless budget (phantom path).
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        ticket_id, worktree, started_at, surface_ref="gone-ref"
    )
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(
        home, worktree, f"uuid-{status}", payload, surface_ref="gone-ref"
    )

    alive = _mk_session("alive-431", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert ticket_id not in report.reverted_ticket_ids, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged terminal)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.COMPLETED, (
        f"status={status!r}: queue task must be COMPLETED, not re-dispatched"
    )


@pytest.mark.parametrize(
    "status",
    [
        "ambiguities_pending_resolution",
        "premises_pending_verification",
        "plan_pending_approval",
        "review_pending_approval",
    ],
)
def test_salvage_paused_statuses_from_phantom_route_to_blocked_on_user(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom session whose transcript emits a paused status must be salvaged and
    the queue task must be set to BLOCKED_ON_USER (not COMPLETED), so downstream
    operators know the session requires human input before re-dispatch (#471).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"471p-{status}"
    worktree = tmp_path / f"wt-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    # Past the spawn grace window but well under headless budget (phantom path).
    now = started_at + timedelta(seconds=SPAWN_GRACE_SECONDS + 60)

    sess = _mk_headless_daemon_session(
        ticket_id, worktree, started_at, surface_ref="gone-ref"
    )
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(
        home, worktree, f"uuid-{status}", payload, surface_ref="gone-ref"
    )

    alive = _mk_session("alive-471", surface_ref="live-ref")
    save_state(CwState(sessions=[sess, alive]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": "live-ref"}],
    )
    with freezegun.freeze_time(now):
        report = reconcile()

    assert ticket_id not in report.reverted_ticket_ids, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged paused)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    assert reloaded.completed_reason == CompletionReason.NORMAL
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"status={status!r}: queue task must be BLOCKED_ON_USER for paused status"
    )


@pytest.mark.parametrize(
    "status",
    [
        "scope_exceeded",
        "forbidden_area",
        "merge_gate_blocked",
    ],
)
def test_salvage_all_terminal_statuses_from_stalled(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled (wall-clock expired) headless session with each non-retry terminal
    status in transcript must be salvaged, NOT reverted to PENDING (#431)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"431s-{status}"
    worktree = tmp_path / f"wts-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-s-{status}", payload)

    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    assert ticket_id not in reverted, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged terminal)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.COMPLETED, (
        f"status={status!r}: queue task must be COMPLETED, not re-dispatched"
    )


@pytest.mark.parametrize(
    "status",
    [
        "ambiguities_pending_resolution",
        "premises_pending_verification",
        "plan_pending_approval",
        "review_pending_approval",
    ],
)
def test_salvage_paused_statuses_from_stalled_route_to_blocked_on_user(
    status: str,
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled headless session with a paused status in transcript must be salvaged
    and the queue task set to BLOCKED_ON_USER, not COMPLETED (#471)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ticket_id = f"471s-{status}"
    worktree = tmp_path / f"wts-{status}"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)  # past HEADLESS_TIMEOUT_SECONDS
    assert (now - started_at).total_seconds() > HEADLESS_TIMEOUT_SECONDS

    sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
    payload = _make_terminal_payload(status, ticket_id)
    _write_salvage_transcript(home, worktree, f"uuid-s-{status}", payload)

    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=ticket_id,
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id=ticket_id,
                )
            ]
        )
    )

    reverted = revert_stalled_headless_sessions(
        state=load_state(), now=now, config=_auto_config()
    )

    assert ticket_id not in reverted, (
        f"status={status!r}: ticket must NOT be reverted to PENDING (salvaged paused)"
    )
    reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
    assert reloaded.status == SessionStatus.COMPLETED, (
        f"status={status!r}: session must be COMPLETED after salvage"
    )
    task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert task.status == QueueItemStatus.BLOCKED_ON_USER, (
        f"status={status!r}: queue task must be BLOCKED_ON_USER for paused status"
    )


def test_salvage_terminal_statuses_constant_is_single_source_of_truth() -> None:
    """Drift guard: _SALVAGE_TERMINAL_STATUSES in reconcile.py must equal
    SALVAGE_TERMINAL_STATUSES from auto_dev_result.py (#431).

    This test exists purely to catch future drift between the two references.
    The implementation makes _SALVAGE_TERMINAL_STATUSES an alias, but this
    assertion ensures no one accidentally re-inlines a narrower set.
    """
    from cw.auto_dev_result import SALVAGE_TERMINAL_STATUSES as _SHARED
    from cw.reconcile import _SALVAGE_TERMINAL_STATUSES as _RECONCILE

    assert _RECONCILE == _SHARED, (
        "_SALVAGE_TERMINAL_STATUSES in reconcile.py drifted from "
        "SALVAGE_TERMINAL_STATUSES in auto_dev_result.py. "
        f"reconcile has {_RECONCILE!r}, shared has {_SHARED!r}"
    )


# Frozen time for backfill tests: session started_at is 60s before frozen now
# (well within HEADLESS_TIMEOUT_SECONDS=3600) so revert_stalled does not fire.
_BACKFILL_FROZEN_NOW = "2026-06-01T12:00:00+00:00"
_BACKFILL_STARTED_AT = datetime(2026, 6, 1, 11, 59, 0, tzinfo=UTC)


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_happy_path(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE DAEMON session matches roster → claude_session_id backfilled."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-backfill"
    sess = _mk_headless_daemon_session(
        "backfill-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    assert sess.claude_session_id is None
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    # Reload from disk to verify persistence
    reloaded = next(s for s in load_state().sessions if s.id == "backfill-1")
    assert reloaded.claude_session_id == full_uuid


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_no_overwrite(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id already set → roster entry does not change it."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    existing = "existing-uuid"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-no-overwrite"
    sess = _mk_headless_daemon_session(
        "no-overwrite-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    sess.claude_session_id = existing
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "no-overwrite-1")
    assert reloaded.claude_session_id == existing


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_not_in_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from roster → claude_session_id stays None."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    worktree = tmp_path / "wt-not-in-roster"
    sess = _mk_headless_daemon_session(
        "not-in-roster-1", worktree, _BACKFILL_STARTED_AT, surface_ref="deadbeef"
    )
    save_state(CwState(sessions=[sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "not-in-roster-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_outage(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roster fetch raises → no backfill, no crash; outage guard holds."""
    import subprocess as _subprocess

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-outage"
    sess = _mk_headless_daemon_session(
        "outage-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))

    def _raise(*args: object, **kwargs: object) -> None:
        raise _subprocess.CalledProcessError(1, "claude")

    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", _raise)
    # Must not raise
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "outage-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_same_tick_liveness(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After backfill, _transcript_recently_active uses the by-id path."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-liveness"
    sess = _mk_headless_daemon_session(
        "liveness-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    assert sess.claude_session_id is None
    save_state(CwState(sessions=[sess]))

    # Write the transcript under the full UUID filename (by-id path)
    _write_idle_transcript(home, worktree, filename=f"{full_uuid}.jsonl")

    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()

    # Session should NOT be flagged silently idle — transcript found via by-id path
    reloaded = next(s for s in load_state().sessions if s.id == "liveness-1")
    assert reloaded.claude_session_id == full_uuid
    # Session still ACTIVE — watchdog did not fire
    assert reloaded.status == SessionStatus.ACTIVE


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_guard_branches(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard branches: surface_ref=None and non-DAEMON sessions are not backfilled."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-guard"

    # DAEMON ACTIVE but surface_ref=None
    daemon_no_ref = _mk_headless_daemon_session(
        "guard-no-ref", worktree, _BACKFILL_STARTED_AT, surface_ref=None
    )
    assert daemon_no_ref.claude_session_id is None

    # USER ACTIVE with matching surface_ref — should NOT be backfilled
    user_sess = _mk_session("guard-user", short_id, status=SessionStatus.ACTIVE)
    # _mk_session defaults to USER origin
    assert user_sess.origin is SessionOrigin.USER

    save_state(CwState(sessions=[daemon_no_ref, user_sess]))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    reconcile()

    state = load_state()
    reloaded_no_ref = next(s for s in state.sessions if s.id == "guard-no-ref")
    reloaded_user = next(s for s in state.sessions if s.id == "guard-user")
    assert reloaded_no_ref.claude_session_id is None
    assert reloaded_user.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_malformed_roster(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-str sessionId in roster is skipped; valid entry still backfills."""
    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    worktree = tmp_path / "wt-malformed"
    sess = _mk_headless_daemon_session(
        "malformed-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Roster has a malformed entry alongside the valid one
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": 12345}, {"sessionId": full_uuid}],
    )
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "malformed-1")
    assert reloaded.claude_session_id == full_uuid


# ---------------------------------------------------------------------------
# Transcript-filename fallback tests (GitHub issue #522)
# When cw dev-queue wait drives a long run with no reconcile ticks, the
# session exits claude agents before backfill fires.  The transcript file
# persists, so _csid_from_transcript provides a last-resort resolution.
# ---------------------------------------------------------------------------


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_transcript_fallback(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from agents map but transcript exists.

    Resolved via transcript fallback.
    """
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]  # "04bf1c48"
    transcript_stem = f"{short_id}-{full_uuid}"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-tx-fallback"
    sess = _mk_headless_daemon_session(
        "tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Agents returns a non-matching session (daemon reachable, but our session exited)
    # An empty list would trigger the outage guard and abort reconcile entirely.
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    # Write transcript named <short_id>-<uuid>.jsonl
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    # Mtime must be after started_at
    after_started = _BACKFILL_STARTED_AT.timestamp() + 1
    os.utime(tx_path, (after_started, after_started))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "tx-fallback-1")
    assert reloaded.claude_session_id == transcript_stem


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_agents_primary_over_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents map takes priority over transcript when both are present."""
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    transcript_stem = f"{short_id}-different-uuid-xxxx"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-agents-primary"
    sess = _mk_headless_daemon_session(
        "agents-primary-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Agents map has the real uuid
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": full_uuid}]
    )
    # Transcript with a different stem — should NOT win
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    after_started = _BACKFILL_STARTED_AT.timestamp() + 1
    os.utime(tx_path, (after_started, after_started))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "agents-primary-1")
    # Agents map value wins
    assert reloaded.claude_session_id == full_uuid


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_no_transcript_fallback(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref absent from agents map and no transcript → csid stays None."""
    short_id = "04bf1c48"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-no-tx"
    sess = _mk_headless_daemon_session(
        "no-tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Non-matching session so outage guard doesn't fire
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    # Manually create the project dir so it exists but is empty
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "no-tx-fallback-1")
    assert reloaded.claude_session_id is None


@freezegun.freeze_time(_BACKFILL_FROZEN_NOW)
def test_backfill_claude_session_id_stale_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript mtime <= started_at (stale reused-worktree) → csid stays None."""
    import os

    full_uuid = "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"
    short_id = full_uuid[:8]
    transcript_stem = f"{short_id}-{full_uuid}"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-stale-tx"
    sess = _mk_headless_daemon_session(
        "stale-tx-fallback-1", worktree, _BACKFILL_STARTED_AT, surface_ref=short_id
    )
    save_state(CwState(sessions=[sess]))
    # Non-matching session so outage guard doesn't fire
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json", lambda: [{"sessionId": other_uuid}]
    )
    tx_path = _write_idle_transcript(
        home, worktree, filename=f"{transcript_stem}.jsonl"
    )
    # Mtime exactly == started_at (not strictly after) — stale guard fires
    stale_mtime = _BACKFILL_STARTED_AT.timestamp()
    os.utime(tx_path, (stale_mtime, stale_mtime))
    reconcile()
    reloaded = next(s for s in load_state().sessions if s.id == "stale-tx-fallback-1")
    assert reloaded.claude_session_id is None


# ---------------------------------------------------------------------------
# Dot-encoding regression tests (GitHub issue #463)
# Worktrees under ~/.cw/ contain a dot in the path segment; Claude Code
# encodes BOTH '/' and '.' as '-'.  The old single-replace produced a
# path mismatch that caused all transcript liveness checks to return False,
# letting the idle watchdog falsely reap actively-working sessions.
# ---------------------------------------------------------------------------


def test_claude_project_dir_encodes_dots_as_dashes() -> None:
    """claude_project_dir replaces both '/' and '.' with '-' (Issue #463).

    For a worktree at /home/u/.cw/wt/abc/auto-dev-1 the encoded segment
    must be '-home-u--cw-wt-abc-auto-dev-1' (double dash for '.cw').
    """
    from cw._util import claude_project_dir as _cpd

    result = _cpd("/home/u/.cw/wt/abc/auto-dev-1")
    assert result.name == "-home-u--cw-wt-abc-auto-dev-1", (
        f"Expected double-dash for .cw segment; got {result.name!r}"
    )


def test_claude_project_dir_matches_verified_real_path() -> None:
    """Exact encoding match for the worktree documented in GitHub issue #463.

    Real worktree: /home/matthew/.cw/wt/7dc983e2/auto-dev-463
    Wrong (single replace): -home-matthew-.cw-wt-7dc983e2-auto-dev-463
    Correct (double replace): -home-matthew--cw-wt-7dc983e2-auto-dev-463
    """
    from cw._util import claude_project_dir as _cpd

    result = _cpd("/home/matthew/.cw/wt/7dc983e2/auto-dev-463")
    assert result.name == "-home-matthew--cw-wt-7dc983e2-auto-dev-463"
    # Confirm the old single-replace encoding is different (would not exist)
    wrong = "/home/matthew/.cw/wt/7dc983e2/auto-dev-463".replace("/", "-")
    assert wrong != result.name


def test_transcript_recently_active_finds_dotted_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_transcript_recently_active returns True for a ~/.cw/-style worktree.

    Regression for Issue #463: the old single-replace produced a path that
    didn't exist on disk, so the guard always returned False and the watchdog
    would reap active sessions.

    The worktree path contains a dot segment ('dot-cw') to reproduce the
    encoding mismatch without depending on the real ~/.cw directory.
    """
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Worktree path with a dot-prefixed segment, mirroring ~/.cw/wt/...
    worktree = tmp_path / ".dot-cw" / "wt" / "abc123" / "auto-dev-1"
    worktree.mkdir(parents=True, exist_ok=True)

    # Build the project dir using the CORRECT double-replace encoding.
    project_dir = claude_project_dir(worktree)
    project_dir.mkdir(parents=True, exist_ok=True)

    # Write a transcript that is "recent" (mtime = now).
    session_uuid = "test-uuid-dotted"
    transcript = project_dir / f"{session_uuid}.jsonl"
    # Content-bearing timestamp (#1076): liveness now prefers this over mtime.
    now_ish = datetime.now(tz=UTC).isoformat()
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": now_ish,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "still working"}],
            },
        }
    )
    transcript.write_text(record + "\n")

    sess = Session(
        id="dotted-wt-sess",
        name="client-a/auto-dev/DOTTED-1",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=tmp_path / "ws"
        ).workspace_path,
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        claude_session_id=session_uuid,
    )

    now = datetime.now(tz=UTC)
    # With the correct encoding the transcript is found → recently active.
    assert _transcript_recently_active(sess, now, window_seconds=60), (
        "Expected transcript to be found for dotted worktree path; "
        f"project_dir={project_dir!r} exists={project_dir.is_dir()}"
    )


def test_awaiting_subagent_finds_dotted_worktree(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_awaiting_subagent returns True for a ~/.cw/-style worktree with pending
    tool_use.

    Regression for Issue #463: single-replace caused _awaiting_subagent to hit
    its early-exit (project_dir not found), returning False and letting the
    watchdog fire on a session mid-subagent.
    """
    from cw.reconcile import _awaiting_subagent

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / ".dot-cw" / "wt" / "def456" / "auto-dev-2"
    worktree.mkdir(parents=True, exist_ok=True)

    project_dir = claude_project_dir(worktree)
    project_dir.mkdir(parents=True, exist_ok=True)

    session_uuid = "test-uuid-subagent"
    transcript = project_dir / f"{session_uuid}.jsonl"

    # Transcript ends with a tool_use with no following tool_result → awaiting.
    tool_use_ts = datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC).isoformat()
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": tool_use_ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_01", "name": "Task", "input": {}}
                ],
            },
        }
    )
    transcript.write_text(record + "\n")

    sess = Session(
        id="dotted-subagent-sess",
        name="client-a/auto-dev/DOTTED-2",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=tmp_path / "ws"
        ).workspace_path,
        worktree_path=worktree,
        surface_ref="live-ref",
        started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        claude_session_id=session_uuid,
    )

    # now is within SUBAGENT_LIVENESS_WINDOW_SECONDS of the tool_use timestamp
    now = datetime(2026, 1, 1, 0, 16, 0, tzinfo=UTC)
    assert _awaiting_subagent(sess, now), (
        "Expected _awaiting_subagent=True for dotted worktree path; "
        f"project_dir={project_dir!r} exists={project_dir.is_dir()}"
    )


# ---------------------------------------------------------------------------
# GitHub #1076: content-timestamp liveness (metadata-only transcript writes)
# ---------------------------------------------------------------------------


def _mk_content_ts_session(
    sid: str, worktree: Path, started_at: datetime, surface_ref: str = "fake-short-id"
) -> Session:
    """Build a DAEMON ACTIVE session for the #1076 content-timestamp tests."""
    return Session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=ClientConfig(
            name="client-a", workspace_path=Path("/tmp/ws")
        ).workspace_path,
        worktree_path=worktree,
        surface_ref=surface_ref,
        started_at=started_at,
    )


def test_transcript_recently_active_ignores_trailing_metadata_write(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing metadata-only write must not falsely resurrect liveness (#1076).

    Claude Code appends non-conversational record types (``ai-title`` etc.)
    that bump the transcript's mtime without representing genuine activity.
    Liveness must be derived from the last content-bearing entry's timestamp,
    not from mtime — otherwise a stale session's counter falsely resets.
    """
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-trailing-meta"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    old_ts = now - timedelta(hours=3)

    _write_transcript_records(
        home,
        worktree,
        [
            {
                "type": "assistant",
                "timestamp": old_ts.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "working"}],
                },
            },
            # Metadata-only record: no "message" field, bumps mtime on write.
            {"type": "ai-title", "title": "Fix the thing"},
        ],
    )

    sess = _mk_content_ts_session("trailing-meta-1", worktree, started_at)

    assert _transcript_recently_active(sess, now, window_seconds=60) is False

    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - timedelta(hours=3).total_seconds()) < 5


def test_transcript_age_seconds_falls_back_to_mtime_without_content_timestamp(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No parseable content timestamp anywhere → fall back to mtime (#1076).

    Reuses ``_write_idle_transcript`` unmodified: its single record is
    content-bearing (assistant + dict message) but carries no "timestamp"
    key, exactly the "no content signal available" fallback case.
    """
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-fallback-mtime"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    _write_idle_transcript(home, worktree)

    sess = _mk_content_ts_session("fallback-mtime-1", worktree, started_at)

    now = datetime.now(tz=UTC)
    assert _transcript_recently_active(sess, now, window_seconds=60) is True
    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert age < 60


def test_transcript_recently_active_widens_to_subagent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283: a stale registered transcript but a fresh sibling subagent
    transcript in the same project dir -> recently active (widened lookup)."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-active"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # Registered transcript (surface_ref-prefixed), stale but resolvable.
    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    # Sibling subagent transcript (own session id, NOT surface_ref-prefixed), fresh.
    sib = _write_idle_transcript(home, worktree, filename="subagent-abc.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-active-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is True


def test_transcript_age_seconds_widens_to_subagent_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283: age is computed from the FRESHER sibling, not the stale registered
    transcript."""
    from cw.reconcile import _transcript_age_seconds

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-age"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    reg = _write_idle_transcript(home, worktree, filename="fake-short-id-sess.jsonl")
    reg_ts = (started_at + timedelta(seconds=60)).timestamp()
    os.utime(reg, (reg_ts, reg_ts))
    sib = _write_idle_transcript(home, worktree, filename="subagent-abc.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-age-1", worktree, started_at)

    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - 30) < 5


def test_transcript_recently_active_ignores_stale_prior_session_transcript(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 regression guard: a sibling transcript from a PRIOR session (mtime
    before started_at) must NOT count -- the mtime > started_at guard is applied
    per-file across the whole glob, not just the registered transcript."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-guard"
    # Session starts at 00:10; a prior-session sibling's mtime (00:09:30) is
    # recent relative to `now` (00:11:00) but predates this session's start.
    started_at = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 11, 0, tzinfo=UTC)

    # No registered transcript for this session (surface_ref glob finds nothing).
    sib = _write_idle_transcript(home, worktree, filename="prior-subagent.jsonl")
    sib_ts = (started_at - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-guard-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is False


def test_project_transcripts_latest_timestamp_isolates_one_bad_sibling(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 fix-cycle-1 regression guard: a stat() failure on ONE sibling
    transcript must not discard max_ts already found from other, valid
    siblings -- locks the per-candidate (not whole-loop) OSError isolation in
    _project_transcripts_latest_timestamp. Pre-fix, any single bad candidate
    aborted the whole glob scan and returned None."""
    from cw.reconcile import _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-partial-fail"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # No registered transcript resolvable for this session (surface_ref glob
    # finds nothing) -- isolates the assertion to the sibling-glob helper.
    good = _write_idle_transcript(home, worktree, filename="subagent-good.jsonl")
    good_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(good, (good_ts, good_ts))
    bad = _write_idle_transcript(home, worktree, filename="subagent-bad.jsonl")
    bad_ts = (now - timedelta(seconds=10)).timestamp()
    os.utime(bad, (bad_ts, bad_ts))

    real_stat = Path.stat
    stat_fail_msg = "simulated stat failure mid-glob"

    def _flaky_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == "subagent-bad.jsonl":
            raise OSError(stat_fail_msg)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    sess = _mk_content_ts_session("widen-partial-fail-1", worktree, started_at)

    assert _transcript_recently_active(sess, now) is True


def test_widened_transcript_timestamp_survives_primary_transcript_oserror(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1283 fix-cycle-1 regression guard: a stat() failure resolving the
    REGISTERED transcript must not prevent the sibling-glob fallback from
    being consulted -- locks the independent-computation ordering in
    _widened_transcript_timestamp. Pre-fix, the primary's OSError propagated
    before project_ts was ever computed."""
    from cw.reconcile import _transcript_age_seconds, _transcript_recently_active

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-widen-primary-fail"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

    # Fresh sibling transcript -- the widened fallback this fix protects.
    sib = _write_idle_transcript(home, worktree, filename="subagent-live.jsonl")
    sib_ts = (now - timedelta(seconds=30)).timestamp()
    os.utime(sib, (sib_ts, sib_ts))

    sess = _mk_content_ts_session("widen-primary-fail-1", worktree, started_at)

    # Force registered-transcript resolution to a nonexistent path so
    # _effective_transcript_timestamp's stat() raises OSError -- same
    # injection technique as test_transcript_age_seconds_oserror_returns_none
    # (tests/test_cli.py).
    fake_path = worktree / "does-not-exist.jsonl"
    monkeypatch.setattr(
        "cw.reconcile._shared._locate_session_transcript",
        lambda *_a, **_kw: fake_path,
    )

    assert _transcript_recently_active(sess, now) is True
    age = _transcript_age_seconds(sess, now)
    assert age is not None
    assert abs(age - 30) < 5


def test_dirty_worktree_push_fires_once_not_per_tick_timed_out(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMED_OUT session with dirty worktree: push fires exactly once, not per tick.

    The first call routes the RUNNING task to BLOCKED_ON_USER and fires the
    push.  The second call finds the task already BLOCKED_ON_USER (not RUNNING)
    and must not fire again (#763).
    """
    wt_path = tmp_path / "wt-storm-to"
    sess = _mk_daemon_session_with_worktree(
        "storm-to", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-to",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-to",
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-to"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_timed_out_tasks()  # tick 1 — routes to BLOCKED_ON_USER, fires push
    revert_timed_out_tasks()  # tick 2 — task is BLOCKED_ON_USER, must not re-fire
    revert_timed_out_tasks()  # tick 3 — still BLOCKED_ON_USER, must not re-fire

    assert len(push_calls) == 1, (
        f"fire_push_notification must fire exactly once, fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_silent_for_already_blocked_task(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal session with task already BLOCKED_ON_USER never fires push (#763)."""
    wt_path = tmp_path / "wt-storm-ab"
    sess = _mk_daemon_session_with_worktree(
        "storm-ab", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-ab",
                    client="client-a",
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id=None,  # already reverted
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-ab"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    for _ in range(3):
        revert_timed_out_tasks()

    assert push_calls == [], (
        f"fire_push_notification must not fire for already-BLOCKED_ON_USER task, "
        f"fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_silent_for_no_task_terminal_session(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal session with dirty worktree, no task: push never fires (#763)."""
    wt_path = tmp_path / "wt-storm-nt"
    sess = _mk_daemon_session_with_worktree(
        "storm-nt", SessionStatus.TIMED_OUT, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(DevQueueStore(tasks=[]))  # no task at all

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-nt"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    for _ in range(3):
        revert_timed_out_tasks()

    assert push_calls == [], (
        f"fire_push_notification must not fire for zombie session with no queue task, "
        f"fired {len(push_calls)} times"
    )


def test_dirty_worktree_push_fires_once_not_per_tick_completed_silent(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLETED session with dirty worktree: push fires exactly once, not per tick.

    Mirrors test_dirty_worktree_push_fires_once_not_per_tick_timed_out but
    exercises the revert_completed_silent_tasks() code path (#763).
    """
    wt_path = tmp_path / "wt-storm-cs"
    sess = _mk_daemon_session_with_worktree(
        "storm-cs", SessionStatus.COMPLETED, wt_path
    )
    save_state(CwState(sessions=[sess]))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="storm-cs",
                    client="client-a",
                    status=QueueItemStatus.RUNNING,
                    session_id="storm-cs",
                )
            ]
        )
    )

    push_calls: list[tuple[str, str]] = []

    def _capture_push(name: str, client: str, **_kw: object) -> None:
        push_calls.append((name, client))

    monkeypatch.setattr("cw.reconcile._deps.fire_push_notification", _capture_push)
    monkeypatch.setattr(
        "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/storm-cs"
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
    )
    monkeypatch.setattr(
        "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: True
    )

    revert_completed_silent_tasks()  # tick 1 — routes to BLOCKED_ON_USER, fires push
    revert_completed_silent_tasks()  # tick 2 — already BLOCKED_ON_USER, no re-fire
    revert_completed_silent_tasks()  # tick 3 — still BLOCKED_ON_USER, no re-fire

    assert len(push_calls) == 1, (
        f"fire_push_notification must fire exactly once on completed-silent path, "
        f"fired {len(push_calls)} times"
    )


# ---------------------------------------------------------------------------
# session.salvage_skipped event tests (GitHub issue #459)
# ---------------------------------------------------------------------------


def test_salvage_skipped_emitted_for_park_marker(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Park-marker session emits session.salvage_skipped."""
    worktree = tmp_path / "wt-parked-sk"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salvage-skip-1", worktree, started_at)
    sess.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="salvage-skip-1",
        client="client-a",
        status=QueueItemStatus.BLOCKED_ON_USER,
        session_id="salvage-skip-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-salvage-skipped",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["session_id"] == "salvage-skip-1"
    assert p["reason"] == _SALVAGE_SKIP_REASON
    assert p["paused_status"] == _SILENTLY_IDLE_REASON
    assert events[0].correlation_id == "salvage-skip-1"


def test_salvage_skipped_not_emitted_for_terminal_sentinel(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session with a real terminal sentinel does NOT emit session.salvage_skipped."""
    worktree = tmp_path / "wt-salvaged"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    sess = _mk_headless_daemon_session("salvage-real-1", worktree, started_at)
    # last_result is None → no park marker; salvage will find a sentinel
    state = CwState(sessions=[sess])
    save_state(state)

    task = TicketTask(
        ticket_id="salvage-real-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="salvage-real-1",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    # Mock _salvage_terminal_result to return a real terminal result so the
    # session bypasses the salvage-skipped gate entirely.
    fake_result = MagicMock()
    fake_result.cost_usd = None
    fake_result.status = "shipped"
    monkeypatch.setattr(
        "cw.reconcile._shared.salvage_terminal_result",
        lambda *_args, **_kwargs: (fake_result, "fake-claude-id"),
    )

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-no-salvage-skip",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 0


def test_compute_worktree_dirty_returns_false_when_get_client_raises(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compute_worktree_dirty returns False when get_client raises (fail-safe)."""
    from cw.reconcile import _compute_worktree_dirty

    monkeypatch.setattr(
        "cw.reconcile._shared.get_client",
        lambda _name: (_ for _ in ()).throw(ValueError("no such client")),
    )
    assert _compute_worktree_dirty("missing-client", "some-branch") is False


def test_salvage_skipped_emitted_with_null_ticket_id(
    tmp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """Park-marked session without auto-dev/ prefix: salvage_skipped, ticket_id=None."""
    worktree = tmp_path / "wt-no-tid"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 1, 5, 0, tzinfo=UTC)

    # Session name without auto-dev/ prefix → ticket_id_for_session returns None.
    # _is_headless requires a cw-context.json with "headless": true.
    context_dir = worktree / ".claude"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "cw-context.json").write_text(
        '{"headless": true, "session_id": "no-tid-sess"}'
    )
    sess = Session(
        id="no-tid-sess",
        name="client-a/impl",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=tmp_path / "ws",
        worktree_path=worktree,
        started_at=started_at,
        last_result={"paused_status": _SILENTLY_IDLE_REASON},
    )
    state = CwState(sessions=[sess])
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=[]))

    revert_stalled_headless_sessions(state=state, now=now, config=_auto_config())

    events = read_events(
        consumer="test-salvage-skip-null-tid",
        event_types=[OrchestratorEventType.SESSION_SALVAGE_SKIPPED],
    )
    assert len(events) == 1
    p = events[0].payload
    assert p["ticket_id"] is None
    assert p["reason"] == _SALVAGE_SKIP_REASON
    assert events[0].correlation_id is None


# ---------------------------------------------------------------------------
# TestSalvageCommittedNoPrSessions (GitHub issue #497)
# ---------------------------------------------------------------------------


class TestDetectPostReviewClean:
    """Unit tests for _detect_post_review_clean."""

    def test_returns_false_when_worktree_path_none(self, tmp_config_dir: Path) -> None:
        """Session with no worktree_path → False."""
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-nopath",
            name="client-a/sess-nopath",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=None,
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert _detect_post_review_clean(sess) is False

    def test_returns_true_when_matching_event_present(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Event with correct session_id and stage=s3_review_complete → True."""
        from cw.events import record_event as _record_event
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-match",
            name="client-a/sess-match",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        _record_event(
            OrchestratorEventType.STAGE_ENTERED,
            {"session_id": "sess-match", "stage": _STAGE_REVIEW_COMPLETE},
        )
        assert _detect_post_review_clean(sess) is True

    def test_returns_false_when_no_matching_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """No matching event → False."""
        from cw.reconcile import _detect_post_review_clean

        sess = Session(
            id="sess-nomatch",
            name="client-a/sess-nomatch",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        # No event written
        assert _detect_post_review_clean(sess) is False

    def test_returns_false_on_read_events_exception(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_events raises → safe False."""
        from cw.reconcile import _detect_post_review_clean

        def _raise(*_a: object, **_kw: object) -> None:
            msg = "disk error"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._shared.read_events", _raise)

        sess = Session(
            id="sess-exc",
            name="client-a/sess-exc",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=tmp_path / "wt",
            surface_ref="ref",
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert _detect_post_review_clean(sess) is False


# ---------------------------------------------------------------------------
# _locate_session_transcript tests (GitHub #541)
# ---------------------------------------------------------------------------


def _make_locate_session(
    *,
    worktree: Path,
    started_at: datetime,
    surface_ref: str | None = "abcd1234",
    claude_session_id: str | None = None,
) -> Session:
    """Build a minimal DAEMON ACTIVE session for locate-transcript tests."""
    return Session(
        id="test-locate",
        name="client-a/impl",
        client="client-a",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=SessionStatus.ACTIVE,
        workspace_path=Path("/tmp/ws"),
        worktree_path=worktree,
        surface_ref=surface_ref,
        claude_session_id=claude_session_id,
        started_at=started_at,
    )


def test_locate_by_csid_returns_path(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id set and file exists → returns that path."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-csid"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        claude_session_id="full-csid-uuid",
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "full-csid-uuid.jsonl"
    transcript.write_text("{}\n")

    result = _locate_session_transcript(sess)
    assert result == transcript


def test_locate_by_csid_missing_file(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude_session_id set but file absent → None."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-csid-missing"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        claude_session_id="missing-csid",
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    # No transcript written.

    result = _locate_session_transcript(sess)
    assert result is None


def test_locate_by_surface_ref_precise_glob(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref set, matching file mtime > started_at → returns Path."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-sref"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "abcd1234-full-uuid.jsonl"
    transcript.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    result = _locate_session_transcript(sess)
    assert result == transcript


def test_locate_excludes_sibling_by_prefix(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused-worktree scenario: sibling transcript (different surface_ref,
    mtime NEWER than target's started_at) does NOT win; the target's own
    transcript is returned.  Proves the prefix-scoping (not just the mtime
    guard) excludes the sibling.  See GitHub #541.
    """
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-sibling"
    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)

    # Sibling transcript: DIFFERENT surface_ref prefix, but mtime AFTER started_at.
    # If only the mtime guard were applied (not the prefix glob), this would be
    # picked as the newest candidate.
    sibling = project_dir / "zzzzzzzz-other-session.jsonl"
    sibling.write_text("{}\n")
    sibling_ts = started_at.timestamp() + 120  # newer than target's started_at
    os.utime(str(sibling), (sibling_ts, sibling_ts))

    # Target transcript: correct surface_ref prefix, mtime after started_at.
    target = project_dir / "abcd1234-full-uuid.jsonl"
    target.write_text("{}\n")
    target_ts = started_at.timestamp() + 60  # after started_at, but older than sibling
    os.utime(str(target), (target_ts, target_ts))

    result = _locate_session_transcript(sess)
    # Must return the TARGET transcript, not the newer sibling.
    assert result == target
    assert result != sibling


def test_locate_stale_mtime_returns_none(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """surface_ref set, matching file exists but mtime <= started_at → None."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-stale-sref"
    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / "abcd1234-uuid.jsonl"
    transcript.write_text("{}\n")
    # Stamp BEFORE started_at.
    stale_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (stale_ts, stale_ts))

    result = _locate_session_transcript(sess)
    assert result is None


def test_locate_no_surface_ref_no_csid(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both surface_ref and claude_session_id are None → None (no fallback)."""
    from cw.reconcile import _locate_session_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "wt-no-ids"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref=None,
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    # Write a transcript that would be picked by an unscoped *.jsonl glob.
    unscoped = project_dir / "some-uuid.jsonl"
    unscoped.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(unscoped), (after_ts, after_ts))

    # Should NOT fall back to unscoped glob — returns None.
    result = _locate_session_transcript(sess)
    assert result is None


def test_awaiting_subagent_stale_transcript_returns_false(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale transcript (mtime <= started_at) with pending tool_use → False.

    Before #541, _awaiting_subagent used an unscoped *.jsonl glob with no
    mtime guard in the surface_ref branch.  A stale transcript with a pending
    tool_use tail would have returned True (false-positive watchdog suppression).
    Now the mtime guard applies uniformly via _locate_session_transcript.
    """
    from cw.reconcile import _awaiting_subagent

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-stale-subagent"
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref="abcd1234",
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)

    # Write a transcript with a pending tool_use tail.
    record = json.dumps(
        {
            "type": "assistant",
            "timestamp": now.isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}],
            },
        }
    )
    transcript = project_dir / "abcd1234-stale.jsonl"
    transcript.write_text(record + "\n")
    # Stamp BEFORE started_at — stale transcript guard should fire.
    stale_ts = started_at.timestamp() - 3600
    os.utime(str(transcript), (stale_ts, stale_ts))

    # Must return False: stale transcript → helper returns None → fail-open.
    assert _awaiting_subagent(sess, now) is False


def test_csid_from_transcript_via_helper(
    tmp_path: Path,
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_csid_from_transcript delegates to _locate_session_transcript; returns stem."""
    from cw.reconcile import _csid_from_transcript

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    worktree = tmp_path / "wt-csid-helper"
    surface_ref = "abcd1234"
    full_stem = f"{surface_ref}-full-uuid-xxxx"
    sess = _make_locate_session(
        worktree=worktree,
        started_at=started_at,
        surface_ref=surface_ref,
        claude_session_id=None,
    )

    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    transcript = project_dir / f"{full_stem}.jsonl"
    transcript.write_text("{}\n")
    after_ts = started_at.timestamp() + 60
    os.utime(str(transcript), (after_ts, after_ts))

    result = _csid_from_transcript(sess)
    assert result == full_stem


# ---------------------------------------------------------------------------
# ReapReason taxonomy tests (GitHub #380)
# One test per reap-site decision-table row.
# ---------------------------------------------------------------------------


class TestEmitReapProposed:
    """_emit_reap_proposed emits SESSION_REAP_PROPOSED and stamps reap_proposed_at."""

    def test_reap_proposed_emits_event_for_revert_task(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """REVERT_TASK candidate → SESSION_REAP_PROPOSED event emitted, stamped."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-revert"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-revert-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-revert-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-revert-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        # Event was emitted
        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        ev = reap_events[0]
        assert ev.payload["session_id"] == "prop-revert-1"
        assert ev.payload["proposed_action"] == "revert_task"
        assert ev.payload["reason"] == "wall_clock_budget"
        assert ev.correlation_id == "prop-revert-1"

        # Session stamped
        assert sess.reap_proposed_at == now

    def test_reap_proposed_emits_event_for_crash_complete(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """CRASH_COMPLETE candidate → SESSION_REAP_PROPOSED event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)

        sess = _mk_session("prop-crash-1", "gone-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.name = "client-a/auto-dev/prop-crash-1"
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-crash-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="prop-crash-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["proposed_action"] == "crash_complete"
        assert sess.reap_proposed_at == now

    def test_reap_proposed_emits_for_park_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """PARK_BLOCKED_ON_USER candidate → SESSION_REAP_PROPOSED event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)

        sess = _mk_session("prop-park-1", "live-ref")
        sess.origin = SessionOrigin.DAEMON
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-park-1",
            proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
            ticket_id="prop-park-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["proposed_action"] == "park_blocked_on_user"

    def test_reap_proposed_skips_non_reap_actions(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """INCREMENT_COUNTER / SKIP_PARKED candidates → no event emitted."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)

        sess = _mk_session("prop-skip-1", "live-ref")
        sess.started_at = started_at
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))
        snap = _state_queue_snapshot()

        candidate = ReapCandidate(
            session_id="prop-skip-1",
            proposed_action=ProposedAction.INCREMENT_COUNTER,
            ticket_id="prop-skip-1",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        assert _state_queue_snapshot() == snap
        assert sess.reap_proposed_at is None

    def test_reap_proposed_dedup_skips_already_stamped(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Session already stamped with reap_proposed_at → no duplicate event."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-dedup"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        first_stamp = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-dedup-1", worktree, started_at)
        sess.reap_proposed_at = first_stamp  # pre-stamped
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-dedup-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-dedup-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        # No event emitted (already proposed)
        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert reap_events == []
        # Stamp NOT overwritten
        assert sess.reap_proposed_at == first_stamp

    def test_reap_proposed_in_roster_sets_in_roster_true(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When surface_ref is in native_live, evidence.in_roster=True."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-roster"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-roster-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-roster-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-roster-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(
            state,
            [candidate],
            native_live={"fake-short-id"},  # matches sess.surface_ref
            now=now,
        )

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["evidence"]["in_roster"] is True

    def test_reap_proposed_not_in_roster_sets_in_roster_false(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """When surface_ref not in native_live, evidence.in_roster=False."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-prop-norost"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-norost-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="prop-norost-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="prop-norost-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["evidence"]["in_roster"] is False

    def test_reap_proposed_empty_candidates_no_writes(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Empty candidates list → no events, no state writes."""
        from cw.reconcile import _emit_reap_proposed

        worktree = tmp_path / "wt-prop-empty"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("prop-empty-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))
        snap = _state_queue_snapshot()

        _emit_reap_proposed(state, [], native_live=set(), now=now)

        assert _state_queue_snapshot() == snap


# ---------------------------------------------------------------------------
# Per-lane reap_policy tests (GitHub #560)
# ---------------------------------------------------------------------------


class TestResolveReapPolicy:
    """resolve_reap_policy respects lane → global → SIGNAL_ONLY precedence."""

    def test_lane_policy_beats_global(self) -> None:
        """Lane AUTO overrides global SIGNAL_ONLY."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients = {
            "client-a": _client_with_lane("client-a", "fast", ReapPolicy.AUTO),
        }
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY)
        candidate = ReapCandidate(
            session_id="s1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t1",
            lane="fast",
            client="client-a",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_global_used_when_lane_not_in_client(self) -> None:
        """Lane name absent from client's lanes → falls back to global."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients = {
            "client-a": _client_with_lane("client-a", "slow", ReapPolicy.SIGNAL_ONLY),
        }
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s2",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t2",
            lane="fast",  # not in client's lanes
            client="client-a",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_global_used_when_client_not_in_dict(self) -> None:
        """Unknown client → falls back to global."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients: dict[str, ClientConfig] = {}
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s3",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t3",
            lane="fast",
            client="unknown-client",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO

    def test_signal_only_failsafe_when_no_client(self) -> None:
        """candidate.client=None + global defaults → SIGNAL_ONLY."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        clients: dict[str, ClientConfig] = {}
        cfg = OrchestratorConfig()  # default: SIGNAL_ONLY
        candidate = ReapCandidate(
            session_id="s4",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t4",
        )
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.SIGNAL_ONLY

    def test_default_lane_resolves_via_global(self) -> None:
        """DEFAULT_LANE not in custom lanes → global policy used."""
        from cw.reconcile import ProposedAction, ReapCandidate, resolve_reap_policy

        lane_cfg = _client_with_lane("client-a", "custom-lane", ReapPolicy.SIGNAL_ONLY)
        clients = {"client-a": lane_cfg}
        cfg = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
        candidate = ReapCandidate(
            session_id="s5",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="t5",
            lane=DEFAULT_LANE,
            client="client-a",
        )
        # "default" lane not in client's declared lanes → global AUTO
        assert resolve_reap_policy(candidate, clients, cfg) is ReapPolicy.AUTO


class TestMixedLanePolicySingleTick:
    """Single reconcile tick with two candidates on different lane policies."""

    def test_mixed_policy_stalled_one_acts_one_signals(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lane A (auto) acts while lane B (signal_only) routes BLOCKED_ON_USER."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree_a = tmp_path / "wt-mixed-a"
        worktree_b = tmp_path / "wt-mixed-b"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_has_unsaved_work", lambda _c, _b: False
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        from cw.models import LaneConfig

        client = ClientConfig(
            name="client-a",
            workspace_path=tmp_path / "ws",
            lanes=[
                LaneConfig(name="fast", reap_policy=ReapPolicy.AUTO),
                LaneConfig(name="slow", reap_policy=ReapPolicy.SIGNAL_ONLY),
            ],
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.load_effective_clients",
            lambda: {"client-a": client},
        )

        sess_a = _mk_headless_daemon_session("mixed-auto-1", worktree_a, started_at)
        sess_b = _mk_headless_daemon_session("mixed-sig-1", worktree_b, started_at)
        state = CwState(sessions=[sess_a, sess_b])
        save_state(state)
        task_a = TicketTask(
            ticket_id="mixed-auto-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="mixed-auto-1",
        )
        task_b = TicketTask(
            ticket_id="mixed-sig-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="mixed-sig-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))

        candidate_a = ReapCandidate(
            session_id="mixed-auto-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="mixed-auto-1",
            elapsed_seconds=3700.0,
            lane="fast",
            client="client-a",
        )
        candidate_b = ReapCandidate(
            session_id="mixed-sig-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="mixed-sig-1",
            elapsed_seconds=3700.0,
            lane="slow",
            client="client-a",
        )

        # Global is SIGNAL_ONLY; lane A is AUTO, lane B is SIGNAL_ONLY
        reverted, _ = _act_on_stalled_candidates(
            state,
            [candidate_a, candidate_b],
            now=now,
            config=OrchestratorConfig(),
        )

        # Lane A (fast/AUTO): reverted to PENDING
        assert "mixed-auto-1" in reverted
        # Lane B (slow/SIGNAL_ONLY): routes to BLOCKED_ON_USER
        assert "mixed-sig-1" not in reverted

        store = load_dev_queue()
        t_a = next(t for t in store.tasks if t.ticket_id == "mixed-auto-1")
        t_b = next(t for t in store.tasks if t.ticket_id == "mixed-sig-1")
        assert t_a.status == QueueItemStatus.PENDING
        assert t_b.status == QueueItemStatus.BLOCKED_ON_USER


class TestReapProposedPayloadLane:
    """SESSION_REAP_PROPOSED payload includes lane field (GitHub #560)."""

    def test_reap_proposed_payload_includes_lane(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Candidate with lane='fast' → payload has lane='fast'."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-lane-payload"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("lane-payload-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="lane-payload-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="lane-payload-1",
            elapsed_seconds=3700.0,
            lane="fast",
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["lane"] == "fast"

    def test_reap_proposed_default_lane_in_payload(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Candidate with default lane → payload has lane='default'."""
        from cw.reconcile import ProposedAction, ReapCandidate, _emit_reap_proposed

        worktree = tmp_path / "wt-default-lane-payload"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("default-lane-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))

        candidate = ReapCandidate(
            session_id="default-lane-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="default-lane-1",
            elapsed_seconds=3700.0,
        )

        _emit_reap_proposed(state, [candidate], native_live=set(), now=now)

        events = read_events()
        reap_events = [
            e for e in events if e.type == OrchestratorEventType.SESSION_REAP_PROPOSED
        ]
        assert len(reap_events) == 1
        assert reap_events[0].payload["lane"] == DEFAULT_LANE


# ---------------------------------------------------------------------------
# GitHub #578 — ROUTE_EMITTED_SENTINEL: reconcile honors emitted-but-unrouted
# sentinels without waiting for signal_stop
# ---------------------------------------------------------------------------


class TestRouteEmittedSentinel:
    """flag_silently_idle_daemon_sessions routes a transcript sentinel before
    the idle watchdog budget fires (GitHub #578)."""

    def test_detect_sentinel_present_routes_before_watchdog(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel present + last_result None + elapsed >= 300 s
        → ROUTE_EMITTED_SENTINEL, task COMPLETED, session COMPLETED."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-detect"
        # 305 s elapsed — past the 300-s check but well under 900-s watchdog.
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)
        assert (now - started_at).total_seconds() >= 300
        assert (now - started_at).total_seconds() < IDLE_WATCHDOG_SECONDS

        sess = _mk_headless_daemon_session("578-detect", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-1", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        # B2: apply_staged_decision needs a pipeline to decide COMPLETED vs advance.
        # Ship at FINALIZE (terminal) → COMPLETED; must have clients.yaml on disk.
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-detect",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-detect",
                        stage=Stage.FINALIZE,  # terminal; shipped here → COMPLETED
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-detect")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "shipped"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-detect")
        assert task.status == QueueItemStatus.COMPLETED

        mock_daemon.stop.assert_called_once_with("fake-short-id")

    def test_detect_elapsed_under_threshold_no_candidate(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """elapsed < sentinel_unrouted_check_seconds → no ROUTE_EMITTED_SENTINEL
        candidate, sentinel left in transcript for next tick."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-under"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 4, 0, tzinfo=UTC)  # 240 s < 300 s threshold
        assert (now - started_at).total_seconds() < 300

        sess = _mk_headless_daemon_session("578-under", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-2", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-under",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-under",
                    )
                ]
            )
        )

        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-under")
        # Session must NOT be completed — too early for the sentinel check.
        assert reloaded.status == SessionStatus.ACTIVE

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-under")
        assert task.status == QueueItemStatus.RUNNING

    def test_detect_last_result_set_skips_double_route(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session.last_result already set → ROUTE_EMITTED_SENTINEL skipped
        (signal_stop already ran; prevents double-routing)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-dbl"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-dbl", worktree, started_at)
        # Simulate: signal_stop already ran and stored last_result.
        sess.last_result = {"status": "shipped"}
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-3", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-dbl",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-dbl",
                    )
                ]
            )
        )

        state = load_state()
        blocked, _salvage = flag_silently_idle_daemon_sessions(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
        )

        assert blocked == []
        # Session stays ACTIVE — watchdog budget not yet exhausted, no route.
        reloaded = next(s for s in load_state().sessions if s.id == "578-dbl")
        assert reloaded.status == SessionStatus.ACTIVE

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-dbl")
        assert task.status == QueueItemStatus.RUNNING

    def test_detect_live_session_with_sentinel_routes_not_crash_complete(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live-roster session + sentinel → ROUTE_EMITTED_SENTINEL, NOT a crash path.
        Session ends COMPLETED/NORMAL (not TIMED_OUT)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-live"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-live", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-4", _no_op_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-live",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-live",
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-live")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "no_op"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-live")
        assert task.status == QueueItemStatus.COMPLETED

    def test_act_blocked_retry_eligible_routes_to_pending(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel status=blocked + retry_eligible=True → task reverts to PENDING."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-retry"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-retry", worktree, started_at)
        sess.last_result = None

        blocked_retry_payload: dict[str, Any] = {
            "schema_version": 4,
            "ticket_id": "578-retry",
            "status": "blocked",
            "stage_reached": "stage2_impl",
            "scope": {
                "tier": "small",
                "files": 0,
                "lines_estimate": 0,
                "lines_actual": None,
                "forbidden_touched": False,
            },
            "plan_source": "generated",
            "branch": None,
            "worktree_path": None,
            "fork_point_sha": None,
            "commits": [],
            "pr": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                "lowest_agent_confidence": "LOW",
                "any_incomplete_risk": True,
                "shortcuts": [],
                "recommendation": "EXIT_FOR_HUMAN_REVIEW",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": {
                "stage": "stage2_impl",
                "reason": "impl_failed",
                "retry_eligible": True,
                "details": "agent timed out",
                "next_actions": ["redispatch_ticket"],
            },
            "next_actions": ["redispatch_ticket"],
        }
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-5", blocked_retry_payload
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-retry",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-retry",
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-retry")
        assert task.status == QueueItemStatus.PENDING
        assert task.session_id is None

    def test_act_signal_only_does_not_gate_route_emitted_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """signal_only policy does NOT block ROUTE_EMITTED_SENTINEL — routing an
        emitted sentinel is constructive, not a destructive reap."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-sigonly"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-sigonly", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-6", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        # B2: apply_staged_decision needs a pipeline to decide COMPLETED vs advance.
        # Ship at FINALIZE (terminal) → COMPLETED; must have clients.yaml on disk.
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-sigonly",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-sigonly",
                        stage=Stage.FINALIZE,  # terminal; shipped here → COMPLETED
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            # signal_only policy — normally gates reaps.
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "578-sigonly")
        # ROUTE_EMITTED_SENTINEL is exempt from signal_only → session COMPLETED.
        assert reloaded.status == SessionStatus.COMPLETED
        task = next(t for t in load_dev_queue().tasks if t.ticket_id == "578-sigonly")
        assert task.status == QueueItemStatus.COMPLETED

    def test_act_session_completed_event_emitted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ROUTE_EMITTED_SENTINEL emits a SESSION_COMPLETED event."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-event"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        sess = _mk_headless_daemon_session("578-event", worktree, started_at)
        sess.last_result = None
        _write_salvage_transcript(
            home, worktree, "claude-578-uuid-7", _shipped_salvage_payload()
        )
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="578-event",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="578-event",
                        # Matches _shipped_salvage_payload()'s stage_reached
                        # ("stage5_post_create") so the #1019/#1031
                        # stage-match guard accepts the route.
                        stage=Stage.FINALIZE,
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        events = read_events(
            consumer="test-578-event",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["session_id"] == "578-event"
        assert payload["status"] == "shipped"
        assert payload["salvaged"] is True
        assert payload["crashed"] is False

    def test_review_pending_approval_sentinel_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: review_pending_approval-shaped sentinel emitted without
        signal_stop routes task to BLOCKED_ON_USER (PAUSED_FOR_USER_INPUT path),
        not stuck as RUNNING indefinitely. See #633."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-578-rpa"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 5, tzinfo=UTC)

        ticket_id = "578-rpa"
        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        sess.last_result = None
        payload = _make_terminal_payload("review_pending_approval", ticket_id)
        _write_salvage_transcript(home, worktree, "claude-578-uuid-8", payload)
        save_state(CwState(sessions=[sess]))
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id=ticket_id,
                        # stage_reached=stage3_review in the payload above must
                        # match the seeded task's stage -- otherwise #1019's
                        # stage-mismatch guard refuses to route it (a task at
                        # the default Stage.PLAN could never realistically
                        # emit a review-stage sentinel).
                        stage=Stage.REVIEW,
                    )
                ]
            )
        )

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            state = load_state()
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == ticket_id)
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.last_result is not None
        assert reloaded.last_result["status"] == "review_pending_approval"

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        # review_pending_approval is in PAUSED_FOR_USER_INPUT_STATUSES (#633).
        assert task.status == QueueItemStatus.BLOCKED_ON_USER

    def test_route_emitted_sentinel_refusal_stops_refiring(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1149: a stage-mismatch refusal stamps a paused_status-only
        marker on session.last_result AND persists it, so the doomed candidate
        is not re-proposed on a subsequent tick (extends #1031's non-orphan
        guard with the actual anti-refiring fix).

        Same shape as ``test_idle_stage_mismatch_does_not_orphan_task_or_
        complete_session`` (task row already advanced past the sentinel's
        stage_reached), but drives the full ``flag_silently_idle_daemon_
        sessions`` act phase end-to-end, then reloads from disk to prove the
        marker survived. This exercises the #1149 save-gate fix: on a
        pure-refusal tick the ``accepted`` routed-sentinel list is empty, so
        ``has_dispositions`` is False; without ``_apply_idle_routed_mutations``
        signalling that it mutated session state, ``_act_on_idle_candidates``
        would skip ``save_state`` and lose the marker, and the candidate would
        re-fire on the next (fresh ``load_state``) tick forever.
        """
        from cw.reconcile import ProposedAction, _detect_idle_candidates

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-refusal"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-refusal", worktree, started_at)
        sess.last_result = None  # sentinel NOT yet consumed -> ROUTE_EMITTED eligible
        payload = _stage_complete_payload()  # stage_reached="stage2_impl" (IMPL)
        payload["ticket_id"] = "1149-refusal"
        _write_salvage_transcript(home, worktree, "claude-1149-refusal", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-refusal",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-refusal",
            # Row already advanced past IMPL by the time this stale IMPL-leg
            # sentinel is discovered -- the #986 shape.
            stage=Stage.REVIEW,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
                task_by_ticket={"1149-refusal": task},
            )

        # Refusal must not complete the still-alive surface, and the marker must
        # be PERSISTED (the #1149 save-gate fix) so the next fresh-load tick sees
        # it. Read back from disk, not the in-memory object.
        mock_daemon.stop.assert_not_called()
        reloaded_state = load_state()
        reloaded = next(s for s in reloaded_state.sessions if s.id == "1149-refusal")
        assert reloaded.status != SessionStatus.COMPLETED
        assert reloaded.last_result == {
            "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }
        task_after = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "1149-refusal"
        )
        assert task_after.stage == Stage.REVIEW
        assert task_after.status == QueueItemStatus.RUNNING

        # Second tick over the reloaded (marked) session: the marker flips
        # `last_result is None` false, so the ROUTE_EMITTED_SENTINEL candidate is
        # not re-proposed -- the refusal loop is broken.
        candidates_2 = _detect_idle_candidates(
            reloaded_state,
            now=now + timedelta(seconds=1),
            native_live={"fake-short-id"},
            config=_auto_config(),
            task_by_ticket={"1149-refusal": task_after},
        )
        assert all(
            c.proposed_action != ProposedAction.ROUTE_EMITTED_SENTINEL
            for c in candidates_2
        )

    def test_route_emitted_sentinel_refusal_marker_is_not_terminal_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #1149 refusal marker carries no "status" key, so
        _has_terminal_sentinel stays False -- the session is not mistaken for
        genuinely terminal and the ordinary idle-stall machinery still runs."""
        from cw.reconcile import _detect_idle_candidates
        from cw.reconcile.idle import _apply_idle_routed_mutations

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-not-terminal"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-not-terminal", worktree, started_at)
        sess.last_result = None
        payload = _stage_complete_payload()
        payload["ticket_id"] = "1149-not-terminal"
        _write_salvage_transcript(home, worktree, "claude-1149-nt", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-not-terminal",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-not-terminal",
            stage=Stage.REVIEW,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"fake-short-id"},
            config=_auto_config(),
            task_by_ticket={"1149-not-terminal": task},
        )
        session_by_id = {s.id: s for s in state.sessions}
        _apply_idle_routed_mutations(session_by_id, candidates, now=now)

        reloaded = session_by_id["1149-not-terminal"]
        assert reloaded.last_result == {
            "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }
        assert _has_terminal_sentinel(reloaded) is False

    def test_idle_later_stage_sentinel_routes_forward_instead_of_looping(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub #1149 R1, idle.py wiring: a later-stage sentinel (a legitimate
        self-escalation the row hasn't caught up to) routes forward via the
        shared staged-advance authority through the full alive-idle sweep
        (flag_silently_idle_daemon_sessions -> _detect_idle_candidates ->
        _apply_idle_routed_mutations) -- it does NOT hit the #1149 R4 refusal
        branch, so no paused_status marker is stamped and the session
        completes normally. Mirrors
        test_phantom_later_stage_sentinel_routes_forward_instead_of_looping's
        coverage for the idle-sweep call path, which was previously untested.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-1149-idle-later"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = started_at + timedelta(seconds=400)

        sess = _mk_headless_daemon_session("1149-idle-later", worktree, started_at)
        sess.last_result = None  # sentinel NOT yet consumed -> ROUTE_EMITTED eligible
        payload = _stage_complete_payload()
        payload["ticket_id"] = "1149-idle-later"
        payload["status"] = "blocked"
        payload["stage_reached"] = "stage4b_pr_create"  # FINALIZE
        payload["blocker"] = {
            "stage": "stage4b_pr_create",
            "reason": "merge_gate_failed",
            "retry_eligible": False,
            "details": "self-escalated",
            "next_actions": [],
        }
        _write_salvage_transcript(home, worktree, "claude-1149-idle-later", payload)
        state = CwState(sessions=[sess])
        save_state(state)
        _write_staged_clients_yaml(tmp_config_dir, "client-a")
        task = TicketTask(
            ticket_id="1149-idle-later",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="1149-idle-later",
            # IMPL is EARLIER than the sentinel's mapped FINALIZE stage ->
            # "later" position: a legitimate self-escalation, walked forward.
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        mock_daemon = MagicMock()
        with patch(
            "cw.reconcile._deps.get_native_daemon_client", return_value=mock_daemon
        ):
            blocked, _salvage = flag_silently_idle_daemon_sessions(
                state,
                now=now,
                native_live={"fake-short-id"},
                config=_auto_config(),
                task_by_ticket={"1149-idle-later": task},
            )

        assert blocked == []
        reloaded = next(s for s in load_state().sessions if s.id == "1149-idle-later")
        assert reloaded.status == SessionStatus.COMPLETED
        assert reloaded.completed_reason == CompletionReason.NORMAL
        assert reloaded.last_result is not None
        assert reloaded.last_result.get("paused_status") is None
        mock_daemon.stop.assert_called_once_with("fake-short-id")

        task_after = next(
            t for t in load_dev_queue().tasks if t.ticket_id == "1149-idle-later"
        )
        # Walk IMPL -> REVIEW -> FINALIZE, then Rule 5 routes the blocked
        # status at the landed FINALIZE stage.
        assert task_after.stage == Stage.FINALIZE
        assert task_after.status == QueueItemStatus.BLOCKED_ON_USER


# ---------------------------------------------------------------------------
# GitHub #637 — world-state check before revert (merged-PR guard)
# ---------------------------------------------------------------------------


class TestWorldStateCheckBeforeRevert:
    """_act_on_stalled/idle/phantom skip revert when PR is already merged."""

    # --- stalled ---

    def test_stalled_merged_ticket_completes_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED session + COMPLETED queue, not TIMED_OUT."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-stalled-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("stalled-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="stalled-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="stalled-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="stalled-merged-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="stalled-merged-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )

        reverted, merged = _act_on_stalled_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            merged_ticket_ids=frozenset({"stalled-merged-1"}),
        )

        assert reverted == []
        assert "stalled-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "stalled-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-stalled-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("crashed") is False

    def test_stalled_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → BLOCKED_ON_USER queue task, NEEDS_ATTENTION."""
        from cw.reconcile import (
            _GH_CHECK_BLOCKED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_stalled_candidates,
        )

        worktree = tmp_path / "wt-stalled-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("stalled-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="stalled-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="stalled-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="stalled-ghblock-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="stalled-ghblock-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
            lane="stalled-ghblock-lane",
        )

        reverted, merged = _act_on_stalled_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            gh_blocked_ticket_ids=frozenset({"stalled-ghblock-1"}),
        )

        assert reverted == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "stalled-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == _GH_CHECK_BLOCKED_REASON
        assert sess.status == SessionStatus.TIMED_OUT

        events = read_events(
            consumer="test-stalled-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "stalled-ghblock-lane"

    # --- idle ---

    def test_idle_merged_ticket_completes_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED session + COMPLETED queue, not TIMED_OUT."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        worktree = tmp_path / "wt-idle-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("idle-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="idle-merged-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="idle-merged-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.IDLE_STALL,
            client="client-a",
        )

        blocked, merged, _salvage = _act_on_idle_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset({("client-a", "idle-merged-1")}),
        )

        assert blocked == []
        assert "idle-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-idle-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert len(events) == 1
        assert events[0].payload.get("crashed") is False

    def test_detect_idle_candidates_merged_finalize_completes_not_salvage(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Merged FINALIZE-stage worktree session (Mode A) completes shipped,
        not SALVAGE_GIT / needs_salvage (#1054)."""
        from cw.reconcile import _act_on_idle_candidates, _detect_idle_candidates

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        wt_path = tmp_path / "wt-idle-merged-finalize"
        wt_path.mkdir(parents=True)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "auto-dev/idle-merged-finalize-1",
        )

        sess = _mk_live_idle_daemon_session(
            "idle-merged-finalize-1",
            "live-ref",
            started_at,
            idle_observation_count=1,
            worktree_path=wt_path,
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-merged-finalize-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-merged-finalize-1",
            stage=Stage.FINALIZE,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=2),
            task_by_ticket={"idle-merged-finalize-1": task},
            merged_client_ticket_ids=frozenset(
                {("client-a", "idle-merged-finalize-1")}
            ),
        )

        blocked, merged, salvage_git = _act_on_idle_candidates(
            state,
            candidates,
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset(
                {("client-a", "idle-merged-finalize-1")}
            ),
        )

        assert blocked == []
        assert salvage_git == []
        assert "idle-merged-finalize-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-merged-finalize-1")
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "shipped"

    def test_idle_merged_finalize_does_not_complete_different_clients_same_ticket(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full pipeline regression (#1054): client-a's merged FINALIZE session
        completes shipped, but a DIFFERENT client's RUNNING task sharing the
        same ticket_id string must NOT be swept into COMPLETED by the
        merged-first candidate's downstream act phase (_act_on_idle_candidates'
        merge split + _apply_idle_queue_mutations' dev-queue sweep both key on
        bare ticket_id pre-#1054; this proves they are now (client, ticket_id)
        scoped end to end, not just at the classify entry point)."""
        from cw.reconcile import _act_on_idle_candidates, _detect_idle_candidates

        ticket_id = "collide-finalize-1"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        wt_path = tmp_path / "wt-idle-collide-finalize"
        wt_path.mkdir(parents=True)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch",
            lambda _p: "auto-dev/collide-finalize-1",
        )

        sess = _mk_live_idle_daemon_session(
            "collide-finalize-1",
            "live-ref",
            started_at,
            idle_observation_count=1,
            worktree_path=wt_path,
        )
        state = CwState(sessions=[sess])
        save_state(state)
        task_a = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="collide-finalize-1",
            stage=Stage.FINALIZE,
        )
        # client-b's unrelated, unmerged task happens to share the ticket_id
        # string -- must survive this tick untouched.
        task_b = TicketTask(
            ticket_id=ticket_id,
            client="client-b",
            status=QueueItemStatus.RUNNING,
            session_id="some-other-live-session",
        )
        save_dev_queue(DevQueueStore(tasks=[task_a, task_b]))

        candidates = _detect_idle_candidates(
            state,
            now=now,
            native_live={"live-ref"},
            config=_auto_config(idle_confirm_observations=2),
            task_by_ticket={ticket_id: task_a},
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)}),
        )

        blocked, merged, salvage_git = _act_on_idle_candidates(
            state,
            candidates,
            now=now,
            config=_auto_config(),
            merged_client_ticket_ids=frozenset({("client-a", ticket_id)}),
        )

        assert blocked == []
        assert salvage_git == []
        assert ticket_id in merged
        assert sess.status == SessionStatus.COMPLETED

        store = load_dev_queue()
        reloaded_a = next(
            t
            for t in store.tasks
            if t.client == "client-a" and t.ticket_id == ticket_id
        )
        reloaded_b = next(
            t
            for t in store.tasks
            if t.client == "client-b" and t.ticket_id == ticket_id
        )
        assert reloaded_a.status == QueueItemStatus.COMPLETED
        assert reloaded_a.disposition == "shipped"
        # The regression this test guards against: pre-fix, this would also
        # read COMPLETED because _apply_idle_queue_mutations matched on bare
        # ticket_id with no client filter.
        assert reloaded_b.status == QueueItemStatus.RUNNING

    def test_idle_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → BLOCKED_ON_USER queue task, NEEDS_ATTENTION."""
        from cw.reconcile import ProposedAction, ReapCandidate, _act_on_idle_candidates

        worktree = tmp_path / "wt-idle-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile._shared.remove_worktree", lambda *_a, **_kw: None
        )

        sess = _mk_headless_daemon_session("idle-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="idle-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="idle-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="idle-ghblock-1",
            proposed_action=ProposedAction.REVERT_TASK,
            ticket_id="idle-ghblock-1",
            elapsed_seconds=3700.0,
            reap_reason=ReapReason.IDLE_STALL,
            lane="idle-ghblock-lane",
        )

        blocked, merged, _salvage = _act_on_idle_candidates(
            state,
            [candidate],
            now=now,
            config=_auto_config(),
            gh_blocked_ticket_ids=frozenset({"idle-ghblock-1"}),
        )

        assert blocked == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "idle-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert sess.status == SessionStatus.TIMED_OUT

        events = read_events(
            consumer="test-idle-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "idle-ghblock-lane"

    # --- phantom ---

    def test_phantom_merged_ticket_completes_not_crashes(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """merged_ticket_ids → COMPLETED+NORMAL phantom, not COMPLETED+CRASHED."""
        from cw.reconcile import (
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-merged-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-merged-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-merged-1",
            worktree_dirty=False,
            client="client-a",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                merged_ticket_ids=frozenset({"phantom-merged-1"}),
            )
        )

        assert reverted == []
        assert "phantom-merged-1" in merged
        assert sess.status == SessionStatus.COMPLETED
        assert sess.completed_reason == CompletionReason.NORMAL

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-merged-1",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        completed = [e for e in events if not e.payload.get("crashed")]
        assert len(completed) == 1

    def test_phantom_gh_blocked_routes_blocked_on_user(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gh_blocked_ticket_ids → phantom skipped, BLOCKED_ON_USER."""
        from cw.reconcile import (
            _GH_CHECK_BLOCKED_REASON,
            ProposedAction,
            ReapCandidate,
            _act_on_phantom_candidates,
        )

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )

        sess = _mk_phantom_daemon_session("phantom-ghblock-1", started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="phantom-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="phantom-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        candidate = ReapCandidate(
            session_id="phantom-ghblock-1",
            proposed_action=ProposedAction.CRASH_COMPLETE,
            ticket_id="phantom-ghblock-1",
            worktree_dirty=False,
            client="client-a",
            lane="phantom-lane",
        )

        reverted, _names, _limited, _salvaged, _results, merged = (
            _act_on_phantom_candidates(
                state,
                [candidate],
                now=now,
                config=_auto_config(),
                gh_blocked_ticket_ids=frozenset({"phantom-ghblock-1"}),
            )
        )

        assert reverted == []
        assert merged == []

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "phantom-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == _GH_CHECK_BLOCKED_REASON
        assert sess.status == SessionStatus.COMPLETED

        events = read_events(
            consumer="test-phantom-ghblock-1",
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
        )
        assert len(events) == 1
        assert events[0].payload["lane"] == "phantom-lane"

    # --- reconcile() pre-pass ---

    def test_reconcile_prepass_merged_pr_populates_completed_ticket_ids(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: merged PR → completed_ticket_ids, task COMPLETED."""
        worktree = tmp_path / "wt-reconcile-merged"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-merged-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-merged-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-merged-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (True, True),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            list,
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-merged-1" in report.completed_ticket_ids
        assert "reconcile-merged-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-merged-1")
        assert t.status == QueueItemStatus.COMPLETED

    def test_reconcile_prepass_gh_unavailable_blocks_not_reverts(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: gh unavailable → task BLOCKED_ON_USER, not PENDING."""
        worktree = tmp_path / "wt-reconcile-ghblock"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        sess = _mk_headless_daemon_session("reconcile-ghblock-1", worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id="reconcile-ghblock-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="reconcile-ghblock-1",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.pr_is_merged_for_ticket",
            lambda _tid, **_kw: (None, False),
        )
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            list,
        )
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            report = reconcile()

        assert "reconcile-ghblock-1" not in report.reverted_ticket_ids

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == "reconcile-ghblock-1")
        assert t.status == QueueItemStatus.BLOCKED_ON_USER

    def test_reconcile_prepass_custom_prefix(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass passes branch='feat/<ticket_id>' when prefix='feat'."""
        ticket_id = "reconcile-feat-1"
        worktree = tmp_path / "wt-reconcile-feat"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # Write clients.yaml with feature_branch_prefix: feat
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"feat/{ticket_id}"]

    def test_reconcile_prepass_passes_cwd(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass resolves cwd from client's workspace_path (#1269)."""
        ticket_id = "reconcile-cwd-1"
        worktree = tmp_path / "wt-reconcile-cwd"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            "    workspace_path: /tmp/ws-feat\n"
            "    default_branch: main\n"
            "    feature_branch_prefix: feat\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_cwd: list[Path | None] = []

        def _capture(
            tid: str, *, branch: str, cwd: Path | None = None, **_kw: object
        ) -> tuple[bool, bool]:
            captured_cwd.append(cwd)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_cwd == [Path("/tmp/ws-feat")]

    def test_reconcile_prepass_default_prefix_fallback(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: no clients.yaml → branch='dev/<ticket>' (default)."""
        ticket_id = "reconcile-default-1"
        worktree = tmp_path / "wt-reconcile-default"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        # No clients.yaml written — load_clients() returns {}

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        captured_branch: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            captured_branch.append(branch)
            return False, True

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert captured_branch == [f"dev/{ticket_id}"]

    def test_reconcile_prepass_dangling_client_skips_gh_call(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """reconcile() pre-pass: clients.yaml populated but missing this
        session's client → skip the gh call entirely (GitHub #1269).

        Distinct from "no clients.yaml at all" (see
        test_reconcile_prepass_default_prefix_fallback, which must still
        call gh with cwd=None): here clients.yaml is populated with a
        *different* client, so this session's client is dangling/config-
        drifted. An unscoped gh call would risk the same cross-repo
        misattribution the ticket describes.
        """
        ticket_id = "reconcile-dangling-1"
        worktree = tmp_path / "wt-reconcile-dangling"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-b:\n"
            "    workspace_path: /tmp/ws-other\n"
            "    default_branch: main\n"
        )

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        save_state(CwState(sessions=[sess]))
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        calls: list[str] = []

        def _capture(tid: str, *, branch: str, **_kw: object) -> tuple[bool, bool]:
            calls.append(tid)
            return True, True  # would falsely report merged if ever called

        monkeypatch.setattr("cw.reconcile._deps.pr_is_merged_for_ticket", _capture)
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        assert calls == []

    def test_stalled_dead_session_wrong_repo_merge_does_not_phantom_reap(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dead stalled session's ticket must not be phantom-reaped as
        merged/shipped just because the gh CLI's ambient CWD (not scoped to
        the client's repo) happens to answer a same-numbered ticket in a
        different repo as merged. Real repro of GitHub #1269: fakes
        ``cw.gh._sp.run`` itself (not ``_deps.pr_is_merged_for_ticket``) so
        the exact code path under test — cwd threading through
        ``pr_is_merged_for_ticket`` — is exercised end to end via the real
        ``reconcile()`` entry point.

        Pre-fix: no cwd is ever passed to the gh subprocess, so every call
        lands in the "ambient" branch below, which reports the ticket
        MERGED — reproducing the incident (task COMPLETED/"shipped",
        SESSION_COMPLETED with reason="phantom_reap_merged").

        Post-fix: the pre-pass and stalled sweep resolve cwd from the
        client's ``workspace_path`` via ``_git_dir``, so the gh calls land
        in the "scoped correctly" branch, which reports the ticket
        genuinely unmerged in client A's own repo — routing the task to
        BLOCKED_ON_USER under the default SIGNAL_ONLY reap policy instead.
        """
        ticket_id = "wrongrepo-1"
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        worktree = tmp_path / "wt-wrongrepo"
        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 1, 0, tzinfo=UTC)

        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n"
            "  client-a:\n"
            f"    workspace_path: {repo_a}\n"
            "    default_branch: main\n"
        )

        def _fake_run(args: list[str], **kwargs: object) -> Any:
            cwd = kwargs.get("cwd")
            scoped_correctly = cwd == repo_a
            result = MagicMock()
            result.returncode = 0
            if "issue" in args:
                # Correctly scoped to client A's repo: ticket has no linked
                # PRs there (genuinely unmerged). Ambient/unscoped: simulate
                # the collision — a same-numbered ticket that IS merged in
                # whatever repo the ambient CWD happens to resolve to.
                refs = [] if scoped_correctly else [{"number": 999}]
                result.stdout = json.dumps({"closedByPullRequestsReferences": refs})
            elif "list" in args:
                result.stdout = json.dumps([])
            else:
                result.stdout = json.dumps(
                    {"state": "OPEN" if scoped_correctly else "MERGED"}
                )
            return result

        monkeypatch.setattr("cw.gh._sp.run", _fake_run)

        sess = _mk_headless_daemon_session(ticket_id, worktree, started_at)
        state = CwState(sessions=[sess])
        save_state(state)
        task = TicketTask(
            ticket_id=ticket_id,
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id=ticket_id,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", FakeNativeDaemonClient
        )
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        monkeypatch.setattr("cw.reconcile.core.complete_timed_out_merged_tasks", list)
        monkeypatch.setattr(
            "cw.reconcile.core.salvage_committed_no_pr_sessions", lambda _c, **_kw: []
        )

        with freezegun.freeze_time(now):
            reconcile()

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.status != QueueItemStatus.COMPLETED
        assert t.disposition != "shipped"

        events = read_events(
            consumer=f"test-{ticket_id}-phantom-reap-guard",
            event_types=[OrchestratorEventType.SESSION_COMPLETED],
        )
        assert not any(e.payload.get("reason") == "phantom_reap_merged" for e in events)


# ---------------------------------------------------------------------------
# _apply_sentinel_to_task — staged advance tests (GitHub issue #698)
# Regression coverage for the reconcile path that was unreachable in B2:
# a plan_pending_approval sentinel with small scope must advance PLAN→IMPL
# via _apply_sentinel_to_task, not fall through to BLOCKED_ON_USER via the
# stale monolith mapping.
# ---------------------------------------------------------------------------


def _plan_pending_approval_sentinel(
    ticket_id: str, scope_tier: str | None
) -> AutoDevResult:
    """Build a valid AutoDevResult for status=plan_pending_approval at stage1_plan.

    Reproduces the real #663 dogfood sentinel: PLAN stage exits before impl
    so lines_actual=None and scope.tier may be None (B2 pre-impl exempt rule).
    """
    return AutoDevResult.model_validate(
        {
            "schema_version": 4,
            "ticket_id": ticket_id,
            "status": "plan_pending_approval",
            "stage_reached": "stage1_plan",
            "scope": {
                "tier": scope_tier,
                "files": 4,
                "lines_estimate": 120,
                "lines_actual": None,
                "forbidden_touched": False,
            },
            "plan_source": "github_issue_existing",
            "branch": None,
            "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
            "health": {
                # None allowed pre-impl (§3.3 scope.tier/confidence exemption)
                "lowest_agent_confidence": None,
                "any_incomplete_risk": False,
                "shortcuts": [],
                "recommendation": "PROCEED",
                "downgrade_applied": False,
            },
            "next_actions": ["user_approve_plan"],
        }
    )


class TestApplySentinelToTaskStagedAdvance:
    """Regression tests for GitHub #698: _apply_sentinel_to_task must use the
    staged advance decision (apply_staged_decision) rather than the stale
    monolith mapping that predates B2.
    """

    def test_plan_pending_approval_small_scope_advances_plan_to_impl(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier='small' → PENDING at IMPL stage.

        This is the exact production failure from #698: the monolith path
        routed this sentinel to BLOCKED_ON_USER; the staged path auto-advances.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-small"
        session_id = "sess-698-small"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier="small")
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING, (
            f"expected PENDING (staged advance), got {t.status!r} — "
            "monolith mapping was BLOCKED_ON_USER (#698)"
        )
        assert t.stage == Stage.IMPL, (
            f"expected stage=IMPL after PLAN→IMPL advance, got {t.stage!r}"
        )

    def test_status_unknown_blocked_does_not_complete_task(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """#750: an unknown-status sentinel must NOT be marked COMPLETED.

        A worker that emitted status='proceed' (not a valid Status) parses to a
        BlockedResult(status_unknown). The old `else → COMPLETED` fallback
        silently marked the ticket shipped despite no branch/PR (the #728 loss).
        It must route to FAILED — never claim false success.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)
        ticket_id = "GH-750"
        session_id = "sess-750"
        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            '{"schema_version": 4, "ticket_id": "GH-750", "status": "proceed"}\n'
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "status_unknown"

        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED, (
            f"unknown-status sentinel must not COMPLETE (silent false-ship); "
            f"got {t.status!r}"
        )

    def test_plan_pending_approval_null_tier_scope_hint_small_advances(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier=None + scope_hint='small' → IMPL.

        Reproduces the #663 dogfood shape: real PLAN sentinels emit tier=None
        (lines_actual unknown pre-impl). The #696 scope_hint fallback must
        reach production via the reconcile path, not just the consume path.
        """
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-null-tier"
        session_id = "sess-698-null-tier"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="small",  # fallback tier source per #696
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier=None)
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.IMPL

    def test_plan_pending_approval_large_scope_blocks(
        self,
        tmp_config_dir: Path,
    ) -> None:
        """plan_pending_approval + scope.tier='large' → BLOCKED_ON_USER (gate)."""
        client_name = "staged-client"
        _write_staged_clients_yaml(tmp_config_dir, client_name)

        ticket_id = "GH-698-large"
        session_id = "sess-698-large"

        task = TicketTask(
            ticket_id=ticket_id,
            client=client_name,
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.PLAN,
            scope_hint="large",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        sentinel = _plan_pending_approval_sentinel(ticket_id, scope_tier="large")
        _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        store = load_dev_queue()
        t = next(t for t in store.tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER


def _blocked_autodev_payload(ticket_id: str) -> dict[str, Any]:
    """Minimal valid AutoDevResult with status='blocked' at IMPL.

    Routes through STAGE_FAILURE (Rule 5) → BLOCKED_ON_USER when applied.
    """
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": "blocked",
        "stage_reached": "stage2_impl",
        "scope": {
            "tier": "small",
            "files": 1,
            "lines_estimate": 10,
            "lines_actual": 5,
            "forbidden_touched": False,
        },
        "plan_source": "github_issue_existing",
        "branch": None,
        "worktree_path": None,
        "fork_point_sha": None,
        "commits": [],
        "pr": None,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "HIGH",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "EXIT_FOR_HUMAN_REVIEW",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": {
            "stage": "stage2_impl",
            "reason": "agent_block",
            "details": "still failing",
        },
        "next_actions": [],
    }


class TestApplySentinelToTaskLateRescue:
    """GitHub #918: a late Stop-hook sentinel must rescue an idle-parked task.

    The idle watchdog can park a still-completing session BLOCKED_ON_USER
    (retaining session_id). When the late sentinel arrives, the widened
    _apply_sentinel_to_task lookup re-finds the parked task and routes it
    through the shared _route_staged_decision. Returns True iff a parked task
    was rescued via the AutoDevResult arm.
    """

    def _seed_parked_task(
        self,
        tmp_config_dir: Path,
        *,
        ticket_id: str,
        session_id: str,
        stage: Stage,
        scope_hint: str = "small",
        status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    ) -> None:
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=status,
            session_id=session_id,  # retained across the park (#918)
            stage=stage,
            scope_hint=scope_hint,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

    def test_late_stage_complete_rescues_parked_task_nonterminal(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked at IMPL + late stage_complete → PENDING at REVIEW; return True."""
        ticket_id, session_id = "GH-918-nonterm", "sess-918-nonterm"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_late_shipped_rescues_parked_task_terminal_preserves_pr_url(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked at FINALIZE + late shipped → COMPLETED + pr_url; return True."""
        ticket_id, session_id = "GH-918-ship", "sess-918-ship"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.FINALIZE,
        )
        sentinel = AutoDevResult.model_validate(_shipped_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "shipped"
        assert t.pr_url == "https://github.com/foo/bar/pull/99"

    def test_late_no_op_rescues_parked_task(self, tmp_config_dir: Path) -> None:
        """Parked + late no_op → COMPLETED disposition='no_op'; return True."""
        ticket_id, session_id = "GH-918-noop", "sess-918-noop"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.PLAN,
        )
        sentinel = AutoDevResult.model_validate(_no_op_salvage_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.COMPLETED
        assert t.disposition == "no_op"

    def test_late_blocked_autodevresult_reparks_parked_task(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked + late AutoDevResult(blocked) → stays BLOCKED_ON_USER; return True.

        The #923 disposition re-stamp is allowed (Comment 2 rule 5) — rescue
        reports True because the AutoDevResult arm handled a parked task.
        """
        ticket_id, session_id = "GH-918-blk", "sess-918-blk"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = AutoDevResult.model_validate(_blocked_autodev_payload(ticket_id))

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition == "blocked"

    def test_late_blocked_result_leaves_parked_task_untouched(
        self, tmp_config_dir: Path
    ) -> None:
        """Parked + late BlockedResult → task untouched; return False (Comment 9).

        A malformed/unknown-status sentinel carries no success signal, so the
        parked task must not be mutated (no false FAILED, no false completion).
        """
        ticket_id, session_id = "GH-918-blkres", "sess-918-blkres"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            f'{{"schema_version": 4, "ticket_id": "{ticket_id}", '
            '"status": "proceed"}\n'
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "status_unknown"

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.disposition is None

    def test_running_task_autodevresult_returns_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING task + stage_complete advances (existing #698) and returns False.

        Only a parked (BLOCKED_ON_USER) rescue reports True; the live RUNNING
        path returns False even though it advances.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-run", "sess-918-run"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_running_blocked_result_unchanged(self, tmp_config_dir: Path) -> None:
        """RUNNING task + validation_failed BlockedResult still routes as today.

        Regression guard on the widened lookup: the BlockedResult arms must run
        only for RUNNING and behave exactly as before (PENDING + clear session).
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-runblk", "sess-918-runblk"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = parse_stdout(
            "<<<AUTO_DEV_RESULT\n"
            "{\n"
            '  "schema_version": 4,\n'
            f'  "ticket_id": "{ticket_id}",\n'
            '  "status": "shipped",\n'
            '  "stage_reached": "stage5_post_create",\n'
            '  "scope": {"tier": "small", "files": 1, "lines_estimate": 10, '
            '"lines_actual": 5, "forbidden_touched": false},\n'
            '  "plan_source": "github_issue_existing",\n'
            '  "branch": "auto-dev/918",\n'
            '  "worktree_path": null,\n'
            '  "fork_point_sha": "abc123",\n'
            '  "commits": ["def456"],\n'
            '  "pr": null,\n'
            '  "review": {"must_fix_initial": 0, "should_fix": 0, '
            '"fix_cycles_used": 0},\n'
            '  "health": {"lowest_agent_confidence": "HIGH", '
            '"any_incomplete_risk": false, "shortcuts": [], '
            '"recommendation": "PROCEED", "downgrade_applied": false, '
            '"fix_loop_escalated": false},\n'
            '  "friction_highlights": [],\n'
            '  "blocker": null,\n'
            '  "next_actions": ["wait_for_ci"]\n'
            "}\n"
            "AUTO_DEV_RESULT>>>"
        )
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "validation_failed"

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None

    def test_running_no_result_emitted_requeues(self, tmp_config_dir: Path) -> None:
        """RUNNING task + transient no_result_emitted → PENDING, session cleared.

        Regression guard on the extracted _route_blocked_result_to_task helper:
        the transient parse-failure branch must still re-queue a RUNNING task.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-918-transient", "sess-918-transient"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = parse_stdout("narrative only, no sentinel block emitted\n")
        assert isinstance(sentinel, BlockedResult)
        assert sentinel.blocker.reason == "no_result_emitted"

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.session_id is None

    def test_late_sentinel_rescues_signoff_parked_task(
        self, tmp_config_dir: Path
    ) -> None:
        """A signoff-parked (AWAITING_OPERATOR_SIGNOFF) task is rescued by a late
        sentinel the same way a BLOCKED_ON_USER task is (#990).

        No signoff is configured on `_write_staged_clients_yaml`'s client, so
        this exercises only the widened membership lookup (touch-point #30)
        -- not the signoff gate re-firing.
        """
        ticket_id, session_id = "GH-990-signoff", "sess-990-signoff"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.IMPL,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_late_sentinel_re_parks_signoff_ticket_at_review_idempotently(
        self, tmp_config_dir: Path
    ) -> None:
        """The realistic signoff-rescue case: a task parked AWAITING_OPERATOR_
        SIGNOFF at Stage.REVIEW (the only stage the gate ever fires at) with
        signoff still configured, re-entering via a late duplicate sentinel.

        Unlike ``test_late_sentinel_rescues_signoff_parked_task`` (which seeds
        an IMPL-stage state unreachable through any real gate-firing path, to
        isolate the widened membership lookup), this seeds the actual
        production state: the gate re-fires on re-entry through
        ``_route_staged_decision`` and re-parks the ticket at
        AWAITING_OPERATOR_SIGNOFF -- a true no-op, not an accidental advance
        to FINALIZE (#990).
        """
        ticket_id, session_id = "GH-990-signoff-review", "sess-990-signoff-review"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.REVIEW,
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        )
        store = load_dev_queue()
        store.tasks[0].signoff = "operator"
        save_dev_queue(store)
        # stage_reached must match the seeded Stage.REVIEW task -- the shared
        # _stage_complete_payload() fixture carries stage_reached=stage2_impl
        # (IMPL), which would now be a stage-mismatch refusal under #1019's
        # guard rather than exercising the #918 rescue arm's signoff re-park.
        payload = _stage_complete_payload()
        payload["stage_reached"] = "stage3_review"
        sentinel = AutoDevResult.model_validate(payload)

        rescued = _apply_sentinel_to_task(ticket_id, session_id, sentinel).rescued

        assert rescued is True
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        assert t.stage == Stage.REVIEW
        assert t.disposition == "signoff_gate"

    def test_late_sentinel_stage_mismatch_refuses_rescue_reports_not_rescued(
        self, tmp_config_dir: Path
    ) -> None:
        """A stale sentinel against a parked task refuses the #918 rescue (#1019).

        Parked at REVIEW; the late sentinel carries stage_reached=stage2_impl
        (a previous IMPL leg). The shared stage-mismatch guard refuses to route
        it -- the task must stay untouched at REVIEW, and rescued must report
        False (no rescue actually happened), not True.
        """
        ticket_id, session_id = "GH-1019-mismatch-parked", "sess-1019-mismatch-parked"
        self._seed_parked_task(
            tmp_config_dir,
            ticket_id=ticket_id,
            session_id=session_id,
            stage=Stage.REVIEW,
        )
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.BLOCKED_ON_USER
        assert t.stage == Stage.REVIEW
        assert t.disposition is None

    def test_running_task_stage_mismatch_returns_not_routed(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + mismatched sentinel → SentinelRouteOutcome(False, False) (#1019).

        The task stays RUNNING (no status transition, no disposition stamp) --
        a true no-op on the mismatch path, mirroring the parked-task refusal.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1019-mismatch-running", "sess-1019-mismatch-running"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.REVIEW,
            scope_hint="small",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.rescued is False
        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.RUNNING
        assert t.stage == Stage.REVIEW
        assert t.disposition is None

    def test_apply_sentinel_to_task_target_none_returns_routed_true(
        self, tmp_config_dir: Path
    ) -> None:
        """No matching task found → SentinelRouteOutcome(rescued=False, routed=True).

        Regression guard: a lookup miss has nothing to refuse, so `routed` must
        stay True -- callers that gate session completion on `routed` (the
        phantom sweep, #1019) must still complete the session in this case,
        matching pre-#1019 behavior for an unmatched ticket_id/session_id pair.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(
            "GH-1019-no-target", "sess-no-target", sentinel
        )

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)


class TestApplySentinelToTaskRoutedFalseFailedRace:
    """GitHub #1189: `routed` must reflect a FAILED-landing BlockedResult write,
    and a lookup miss must distinguish "raced to terminal" from "no such task".

    Pre-#1189, ``_route_blocked_result_to_task`` returned ``None`` and its sole
    call site discarded the value, so ``routed`` stayed at its default ``True``
    even when the call just landed the task terminal-FAILED. Separately, the
    lookup-miss branch conflated "no matching task at all" with "a matching
    task exists but is outside OCCUPIED_LANE_STATUSES" (raced to terminal by a
    concurrent caller) -- both returned ``routed=True``. Both must now report
    ``routed=False`` so callers do not complete/rescue a session whose task
    independently landed FAILED.
    """

    def test_running_task_blocked_result_deterministic_failure_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + deterministic parse-failure BlockedResult → routed=False."""
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-schema", "sess-1189-schema"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="schema_version_unsupported",
                details="test: unsupported schema_version",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        assert outcome.rescued is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the last_blocked_result diagnostic write is scoped to
        # the unrecognized-reason catch-all only -- this deterministic-parse-
        # failure branch is untouched and must leave it unset.
        assert t.last_blocked_result is None

    def test_running_task_blocked_result_validation_failed_at_cap_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + validation_failed at the attempt cap → routed=False, FAILED."""
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-vfcap", "sess-1189-vfcap"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=_VALIDATION_FAILED_MAX_ATTEMPTS,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_VALIDATION_FAILED,
                details="test: validation failed at cap",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the last_blocked_result diagnostic write is scoped to
        # the unrecognized-reason catch-all only -- this validation_failed-
        # at-cap branch is untouched and must leave it unset.
        assert t.last_blocked_result is None

    def test_running_task_blocked_result_unknown_reason_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """RUNNING + unrecognised blocker reason → routed=False, FAILED.

        Companion/superset of ``test_signal_stop_unknown_blocker_reason_marks_
        failed`` (tests/test_cli.py), which pins the same call through the real
        CLI-adjacent path and now also asserts ``outcome.routed is False``.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-unknown", "sess-1189-unknown"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
            attempts=1,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="unknown_reason_xyz",
                details="test: unrecognised reason code",
            )
        )

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        # GitHub #1266: the unrecognized-reason catch-all now persists the
        # rejected sentinel.
        assert t.last_blocked_result == sentinel.model_dump(mode="json")

    def test_route_blocked_result_catch_all_writes_last_blocked_result(
        self, tmp_config_dir: Path
    ) -> None:
        """GitHub #1266: the unrecognized-reason catch-all persists the
        rejected sentinel.

        Calls _route_blocked_result_to_task directly with an
        unrecognized-reason BlockedResult. Confirms the existing FAILED/
        abandoned landing is unchanged AND the new diagnostic write lands
        so an operator can distinguish "no sentinel yet"
        (last_blocked_result=None) from "a rejected sentinel landed this
        FAILED."
        """
        from cw.reconcile._shared import _route_blocked_result_to_task

        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        target = TicketTask(
            ticket_id="GH-1266-catchall",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-1266-catchall",
            stage=Stage.IMPL,
            attempts=1,
        )
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason="status_unknown",
                details="test: unresolved placeholder sentinel",
            )
        )

        routed = _route_blocked_result_to_task(target, sentinel)

        assert routed is False
        assert target.status == QueueItemStatus.FAILED
        assert target.disposition == "abandoned"
        assert target.last_blocked_result == sentinel.model_dump(mode="json")

    def test_apply_sentinel_to_task_race_already_failed_task_returns_routed_false(
        self, tmp_config_dir: Path
    ) -> None:
        """A same-ticket/session task already raced to FAILED → routed=False.

        Simulates a concurrent winner's ``_route_blocked_result_to_task`` write
        landing FAILED just before this caller's own lookup runs. The task row
        must be left byte-for-byte unchanged -- this call has nothing to route.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-race", "sess-1189-race"
        completed_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
            completed_at=completed_at,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        # An arbitrary valid sentinel -- must never reach the dispatch branch,
        # since the lookup misses (task is outside OCCUPIED_LANE_STATUSES).
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome == SentinelRouteOutcome(rescued=False, routed=False)
        t = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert t.status == QueueItemStatus.FAILED
        assert t.disposition == "abandoned"
        assert t.completed_at == completed_at

    def test_apply_sentinel_to_task_race_miss_logs_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A race-miss (matched-but-excluded lookup) logs a WARNING.

        Companion to ``test_apply_sentinel_to_task_race_already_failed_task_
        returns_routed_false``: this pins the operator-visibility signal
        added alongside that fix -- before it, this branch resolved with no
        signal at all distinguishing it from "no such task ever existed."
        """
        import logging

        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-race-log", "sess-1189-race-log"
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        with caplog.at_level(logging.WARNING):
            outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is False
        assert any(
            "sentinel_race_miss_detected" in rec.message for rec in caplog.records
        )
        assert any(ticket_id in rec.message for rec in caplog.records)

    def test_apply_sentinel_to_task_unrelated_task_present_still_returns_routed_true(
        self, tmp_config_dir: Path
    ) -> None:
        """A non-empty store with no matching row still reports routed=True.

        Companion to ``test_apply_sentinel_to_task_target_none_returns_routed_
        true`` (which pins the fully-empty-store case): this pins the
        non-empty-store, no-match variant, proving the loop doesn't
        false-positive the race flag on an unrelated row.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        other_task = TicketTask(
            ticket_id="GH-1189-other",
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id="sess-1189-other",
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[other_task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(
            "GH-1189-no-match", "sess-1189-no-match", sentinel
        )

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)

    def test_lookup_excluded_row_before_occupied_row_still_routes(
        self, tmp_config_dir: Path
    ) -> None:
        """An excluded-status row preceding the occupied match must not short-
        circuit the loop to a false race-miss (post-review amendment A2).

        Seeds TWO tasks sharing the same ticket_id+session_id shape -- an
        excluded-status (FAILED) row ordered before the occupied (RUNNING)
        row -- and asserts the loop keeps scanning and routes to the occupied
        match rather than reporting routed=False on the first (excluded) hit.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-order", "sess-1189-order"
        excluded_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
        )
        occupied_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.RUNNING,
            session_id=session_id,
            stage=Stage.IMPL,
        )
        save_dev_queue(DevQueueStore(tasks=[excluded_row, occupied_row]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome.routed is True
        t = next(
            t
            for t in load_dev_queue().tasks
            if t.ticket_id == ticket_id and t.status != QueueItemStatus.FAILED
        )
        assert t.status == QueueItemStatus.PENDING
        assert t.stage == Stage.REVIEW

    def test_lookup_terminal_row_with_cleared_session_id_falls_to_true_miss(
        self, tmp_config_dir: Path
    ) -> None:
        """A terminal row whose session_id was cleared must not match the
        excluded-status branch (post-review amendment A3, soundness pin).

        The R3 lookup split's safety rests on an assumed precondition: every
        write that lands a task terminal with a cleared session_id
        (``cancel_ticket``/``cancel_task_for_session``/the PENDING branches of
        ``_route_blocked_result_to_task``) clears ``session_id`` in the SAME
        write as the status transition, so a terminal row should never carry a
        session_id that still matches an in-flight caller's lookup. This test
        exercises that assumed precondition directly (not a system-wide
        guarantee across every write site): given a CANCELLED task with
        session_id=None, the lookup must fall through to the truly-absent
        (routed=True) case, not the excluded-status-match (routed=False) case.
        """
        _write_staged_clients_yaml(tmp_config_dir, "staged-client")
        ticket_id, session_id = "GH-1189-cleared", "sess-1189-cleared"
        stale_row = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.CANCELLED,
            session_id=None,
            stage=Stage.IMPL,
            disposition="cancelled",
        )
        save_dev_queue(DevQueueStore(tasks=[stale_row]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        outcome = _apply_sentinel_to_task(ticket_id, session_id, sentinel)

        assert outcome == SentinelRouteOutcome(rescued=False, routed=True)


# ---------------------------------------------------------------------------
# _parse_sentinel_from_blocks — last-match + documented-example skip (#591)
# ---------------------------------------------------------------------------


def _documented_example_salvage_payload() -> dict[str, Any]:
    """Payload matching the illustrative example in the /auto-dev skill prompt."""
    return {
        "schema_version": 4,
        "ticket_id": "PROJ-1234",
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 42,
            "lines_actual": 47,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/proj-1234-fix-login",
        "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
        "fork_point_sha": "abc1234",
        "commits": ["sha1", "sha2"],
        "pr": {
            "number": 42,
            "url": "https://github.com/.../pull/42",
            "auto_merge": True,
            "base": "main",
        },
        "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "MEDIUM",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["wait_for_ci"],
    }


def _make_sentinel_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an assistant record wrapping *payload* as a sentinel text block."""
    body = json.dumps(payload)
    frame = f"<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"narrative\n{frame}"}],
        },
    }


def test_parse_sentinel_from_blocks_last_match_skips_documented_example(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-block: documented example first, real sentinel second → real one returned.

    Regression for GitHub #591: the live-guard monitor latched onto the
    illustrative pr=42/PROJ-1234 example block instead of the real result.
    last-match + is_documented_example skip must return the real sentinel.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-591"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    example_record = _make_sentinel_record(_documented_example_salvage_payload())
    path = _write_salvage_transcript(
        home,
        worktree,
        "claude-591",
        _shipped_salvage_payload(),
        extra_records=[example_record],
    )
    sess = _mk_headless_daemon_session("591", worktree, started_at)
    save_state(CwState(sessions=[sess]))

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.ticket_id == "salv-1"
    assert result.pr is not None
    assert result.pr.number == 99


def test_parse_sentinel_from_blocks_example_only_returns_none(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the documented example block present → treated as no sentinel (None).

    Regression for GitHub #591: a freshly-spawned session with only the
    prompt's illustrative block should not report as shipped.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt-591b"
    started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    path = _write_salvage_transcript(
        home,
        worktree,
        "claude-591b",
        _documented_example_salvage_payload(),
    )
    sess = _mk_headless_daemon_session("591b", worktree, started_at)
    save_state(CwState(sessions=[sess]))

    result = _parse_sentinel_from_blocks(path)
    assert result is None


# ---------------------------------------------------------------------------
# _parse_sentinel_from_blocks — placeholder doc-example skip (GitHub #1266)
# ---------------------------------------------------------------------------

# Verbatim auto-dev-impl.md "Stage 2 Completion" worked example: ticket_id
# and status are angle-bracket placeholders. Fails schema validation (status
# not a known enum value) and, absent the fix, would parse to
# BlockedResult(status_unknown) -- landing a RUNNING task terminal-FAILED.
_PLACEHOLDER_EXAMPLE_BLOCK = (
    "<<<AUTO_DEV_RESULT\n"
    "{\n"
    '  "schema_version": 4,\n'
    '  "ticket_id": "<ticket-id>",\n'
    '  "status": "<stage_complete | blocked>",\n'
    '  "stage_reached": "stage2_impl",\n'
    '  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0,'
    ' "lines_actual": 0, "forbidden_touched": false},\n'
    '  "plan_source": "<github_issue_existing | generated | free_text | none>",\n'
    '  "branch": "<branch-name>",\n'
    '  "worktree_path": "<session worktree path>",\n'
    '  "fork_point_sha": "<fork point sha>",\n'
    '  "commits": ["<sha1>", "<sha2>"],\n'
    '  "pr": null,\n'
    '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
    '  "health": {\n'
    '    "lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",\n'
    '    "any_incomplete_risk": false,\n'
    '    "shortcuts": [],\n'
    '    "recommendation": "PROCEED",\n'
    '    "downgrade_applied": false,\n'
    '    "fix_loop_escalated": false\n'
    "  },\n"
    '  "friction_highlights": [],\n'
    '  "ambiguities": [],\n'
    '  "blocker": null,\n'
    '  "prior_pr_warnings": [],\n'
    '  "next_actions": []\n'
    "}\n"
    "AUTO_DEV_RESULT>>>"
)


def _write_raw_sentinel_transcript(path: Path, texts: list[str]) -> None:
    """Write a transcript at *path*, one assistant-text record per entry.

    Takes pre-built raw sentinel-block text directly rather than a payload
    dict -- GitHub #1266's placeholder tests need exact fidelity to the
    verbatim doc-example text, which is not valid against any AutoDevResult
    payload shape (its values are unresolved template placeholders).
    """
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"narrative\n{text}"}],
            },
        }
        for text in texts
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_parse_sentinel_from_blocks_placeholder_example_only_returns_none(
    tmp_path: Path,
) -> None:
    """GitHub #1266: only the doc-example placeholder block present -> None.

    Absent the raw-text pre-parse skip, this would parse to
    BlockedResult(status_unknown) since is_documented_example() cannot
    inspect a BlockedResult's fields (it has none matching the check).
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK])

    assert _parse_sentinel_from_blocks(path) is None


def test_parse_sentinel_from_blocks_placeholder_then_real_returns_real(
    tmp_path: Path,
) -> None:
    """GitHub #1266: placeholder block then a real sentinel -> the real result.

    Confirms last-match semantics survive the new placeholder skip.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    real_payload = _stage_complete_payload()
    real_frame = f"<<<AUTO_DEV_RESULT\n{json.dumps(real_payload)}\nAUTO_DEV_RESULT>>>"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK, real_frame])

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.status == "stage_complete"


def test_placeholder_sentinel_does_not_fail_running_task(tmp_path: Path) -> None:
    """GitHub #1266 acceptance test: a placeholder-only transcript must never
    reach _apply_sentinel_to_task and land a RUNNING task FAILED.

    Two-part proof (a literal "call _parse_sentinel_from_blocks, then call
    _apply_sentinel_to_task" chain is not a valid third step here: the scan
    correctly returns None for a placeholder-only transcript, and
    _apply_sentinel_to_task's own signature -- sentinel: AutoDevResult |
    BlockedResult, no Optional -- rejects None. A live call with a None
    result would be a mypy --strict violation, not a demonstration; the
    None return, combined with that signature, IS the safety proof):
    1. Directly parsing the verbatim placeholder text (bypassing the scan
       loop's skip) confirms it WOULD produce BlockedResult(status_unknown)
       -- documents the bug mechanism this fix closes.
    2. The scan loop (_parse_sentinel_from_blocks) returns None for a
       placeholder-only transcript. Every real ROUTE_EMITTED_SENTINEL caller
       (idle.py, stalled.py, phantom.py) guards on `if parsed is not None`
       before invoking _apply_sentinel_to_task, so a None return makes the
       call structurally skipped in production -- and, per the signature
       above, structurally impossible to make otherwise. A RUNNING task can
       therefore never be routed to FAILED off this block.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    # Part 1: prove the bug mechanism (unresolved placeholder -> status_unknown).
    direct = parse_stdout(_PLACEHOLDER_EXAMPLE_BLOCK)
    assert isinstance(direct, BlockedResult)
    assert direct.blocker.reason == "status_unknown"

    # Part 2: the scan loop must never surface it as a routable result.
    path = tmp_path / "transcript.jsonl"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK])
    assert _parse_sentinel_from_blocks(path) is None


def test_1258_shape_placeholder_then_delayed_real_sentinel(tmp_path: Path) -> None:
    """GitHub #1266 / #1258 timeline: placeholder at T, real sentinel at T+1.

    A worker quotes the doc example while narrating, then emits the real
    stage_complete sentinel later in the same transcript -- the scan must
    return the real result, not None and not the placeholder's BlockedResult.
    """
    from cw.reconcile._shared import _parse_sentinel_from_blocks

    path = tmp_path / "transcript.jsonl"
    real_payload = _stage_complete_payload()
    real_frame = f"<<<AUTO_DEV_RESULT\n{json.dumps(real_payload)}\nAUTO_DEV_RESULT>>>"
    _write_raw_sentinel_transcript(path, [_PLACEHOLDER_EXAMPLE_BLOCK, real_frame])

    result = _parse_sentinel_from_blocks(path)
    assert isinstance(result, AutoDevResult)
    assert result.status == "stage_complete"
    assert result.ticket_id == real_payload["ticket_id"]


class TestParseAnySentinelFromTranscript:
    """Two-layer transcript search: csid-exact (Layer 1) then surface_ref (Layer 2).

    Regression for GitHub #892: a REVIEW worker that emits review_pending_approval
    and then spawns fanout subagents gets a new csid (V2) backfilled from the
    daemon roster.  The sentinel lives in the pre-resume V1 transcript; Layer 2
    must fall through to find it when V1 does not match the csid-exact path.
    """

    _SURFACE_REF = "surf1234"
    _STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def _write_sentinel_transcript(
        self, path: Path, status: str, ticket_id: str
    ) -> None:
        """Write a minimal sentinel-bearing JSONL to path."""
        payload = _make_terminal_payload(status, ticket_id)
        body = json.dumps(payload)
        frame = f"<<<AUTO_DEV_RESULT\n{body}\nAUTO_DEV_RESULT>>>\n"
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": frame}],
            },
        }
        path.write_text(json.dumps(record) + "\n")

    def _write_no_sentinel_transcript(self, path: Path) -> None:
        """Write a JSONL with no sentinel framing."""
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "narrative only, no sentinel"}],
            },
        }
        path.write_text(json.dumps(record) + "\n")

    def _mk_session(self, worktree: Path, csid: str | None = None) -> Session:
        return Session(
            id="892-sess",
            name="client-a/auto-dev/892",
            client="client-a",
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            status=SessionStatus.ACTIVE,
            workspace_path=Path("/tmp/ws"),
            worktree_path=worktree,
            surface_ref=self._SURFACE_REF,
            claude_session_id=csid,
            started_at=self._STARTED_AT,
        )

    def test_v2_csid_no_sentinel_v1_surface_ref_has_sentinel_returns_v1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Layer 2 fallthrough: csid transcript found but no sentinel; surface_ref
        transcript (distinct file) carries review_pending_approval → returned.

        Regression for #892: REVIEW worker emits sentinel in V1, then spawns
        subagents.  Backfill sets claude_session_id to V2 (resumed session).
        V2 transcript has no sentinel.  Layer 2 must find and return V1.
        """
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-layer2"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        # V2: csid transcript (distinct csid — does not match surface_ref glob)
        v2_csid = "v2-resumed-csid-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_no_sentinel_transcript(v2_path)

        # V1: surface_ref transcript (matches "surf1234*.jsonl" glob, has sentinel)
        v1_path = project_dir / f"{self._SURFACE_REF}-v1original.jsonl"
        self._write_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "review_pending_approval"
        assert csid_stem == f"{self._SURFACE_REF}-v1original"

    def test_neither_transcript_has_sentinel_returns_none(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both csid transcript and surface_ref transcript lack a sentinel → None."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-none"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v2_csid = "v2-no-sentinel-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_no_sentinel_transcript(v2_path)

        v1_path = project_dir / f"{self._SURFACE_REF}-v1-also-none.jsonl"
        self._write_no_sentinel_transcript(v1_path)

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is None

    def test_csid_transcript_with_sentinel_wins_over_surface_ref(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: when csid transcript has a sentinel, Layer 1 returns it
        immediately without consulting surface_ref (Layer 2 never fires)."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-layer1-wins"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        # csid transcript has sentinel (plan_pending_approval)
        v2_csid = "v2-has-sentinel-892"
        v2_path = project_dir / f"{v2_csid}.jsonl"
        self._write_sentinel_transcript(v2_path, "plan_pending_approval", "892-plan")

        # surface_ref transcript also has a sentinel (review_pending) — should NOT win
        v1_path = project_dir / f"{self._SURFACE_REF}-v1-review.jsonl"
        self._write_sentinel_transcript(v1_path, "review_pending_approval", "892")

        sess = self._mk_session(worktree, csid=v2_csid)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert csid_stem == v2_csid

    def test_csid_absent_surface_ref_only_returns_sentinel(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No csid set — existing csid=None path still works via Layer 2 only."""
        from cw.reconcile._shared import _parse_any_sentinel_from_transcript

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        worktree = tmp_path / "wt-892-no-csid"
        project_dir = claude_project_dir(worktree)
        project_dir.mkdir(parents=True)

        v1_path = project_dir / f"{self._SURFACE_REF}-original.jsonl"
        self._write_sentinel_transcript(v1_path, "plan_pending_approval", "892-plan")

        sess = self._mk_session(worktree, csid=None)
        result = _parse_any_sentinel_from_transcript(sess)

        assert result is not None
        parsed, csid_stem = result
        assert isinstance(parsed, AutoDevResult)
        assert parsed.status == "plan_pending_approval"
        assert csid_stem == f"{self._SURFACE_REF}-original"


def _write_gate_orchestrator_yaml(*, gate_recipes_enabled: bool) -> None:
    """Write orchestrator.yaml toggling the gate-recipe master switch.

    concierge_enabled stays False so the tick exercises only the gate-recipe
    path (single-concern), and reconcile()'s real load_orchestrator_config()
    reads this file off disk rather than an in-memory _config() object.
    """
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"gate_recipes_enabled: {str(gate_recipes_enabled).lower()}\n"
        "concierge_enabled: false\n"
    )


def _stub_gate_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub cw.gh._sp.run so the post-approve comment never hits real gh."""

    def _fake_run(
        argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("cw.gh._sp.run", _fake_run)


class TestReconcileGateRecipeIntegration:
    """RFC 0009 (#1088 item 2): behavioral reconcile()-level coverage of the
    gate-recipe act path — the real load_orchestrator_config() disk read,
    per-lane enablement, gate advance, and event emission through ONE unmocked
    tick. The sibling TestConciergeAndEscalationWiring is mocked wiring-only
    (asserts the sweeps are *called*); this drives the reactor for real.

    The owning session is refless (surface_ref=None) so compute_drift skips it
    (no phantom → the cheap no-phantoms branch of _reconcile_locked, which still
    runs run_gate_recipes via _run_terminal_backstops_and_sweeps).
    """

    @staticmethod
    def _blocked_task(stage: Stage) -> TicketTask:
        return TicketTask(
            ticket_id="GEN-1",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=stage,
            session_id="sess-1",
        )

    def test_clean_review_auto_approved_advances_to_finalize(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.FINALIZE
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_APPROVE_REVIEW

    def test_clean_plan_auto_adopted_advances_to_impl(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=True)
        _stub_gate_comment(monkeypatch)
        stub_fetch_plan(monkeypatch, plan_body())
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.PLAN)]))
        save_state(CwState(sessions=[_make_session(last_result=_plan_result())]))

        reconcile()

        store = load_dev_queue()
        assert store.tasks[0].stage == Stage.IMPL
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["recipe"] == RECIPE_AUTO_ADOPT_PLAN

    def test_master_switch_off_leaves_task_blocked(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_acme_clients_yaml(tmp_config_dir, tmp_path)
        _write_gate_orchestrator_yaml(gate_recipes_enabled=False)
        _stub_gate_comment(monkeypatch)
        save_dev_queue(DevQueueStore(tasks=[self._blocked_task(Stage.REVIEW)]))
        save_state(CwState(sessions=[_make_session(last_result=_clean_result())]))

        reconcile()

        store = load_dev_queue()
        # Recipe never fired: the row stays parked at the review gate. (Do not
        # assert escalation_parked_at — the escalation sweep may stamp it in the
        # same tick; only the gate-recipe non-fire is under test here.)
        assert store.tasks[0].status == QueueItemStatus.BLOCKED_ON_USER
        assert store.tasks[0].stage == Stage.REVIEW
        events = read_events(
            event_types=[OrchestratorEventType.GATE_AUTO_APPROVED],
        )
        assert events == []
